from __future__ import annotations
"""
Upstox market-data source — authenticated PRIMARY for daily OHLCV + quotes.

Unlike the NSE/yfinance scrapers, this is an *authenticated* client (Bearer
access token), so it does not hit the anti-bot 403 wall that
``nse_fetcher`` does and is not subject to Yahoo's empty-body throttling.

Requires a daily ``upstox_access_token`` in Settings (Upstox OAuth tokens
expire ~03:30 IST daily — same UX as the existing Kite adapter, which also
needs a daily ``kite_access_token``).

Endpoints (Upstox API v2 — verified against developer docs):
  - Historical:  GET /v2/historical-candle/{instrument_key}/{interval}/{to}/{from}
                 candle = [timestamp, open, high, low, close, volume, oi]
  - Quote:       GET /v2/market-quote/quotes?instrument_key=NSE_EQ|<ISIN>
                 data keyed as "NSE_EQ:<SYMBOL>" (colon separator)
  - Instruments: https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz

Everything fails *closed* — any error returns ``None`` so the caller
(``data_fetcher``) cascades to the next source. No exception escapes.
"""
import asyncio
import gzip
import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_BASE = "https://api.upstox.com/v2"
_INSTRUMENT_URL = "https://assets.upstox.com/market-quote/instruments/exchange/{exch}.json.gz"
_HTTP_TIMEOUT = 12.0

# OAuth2 authorization-code endpoints (Upstox API v2).
_LOGIN_DIALOG = "https://api.upstox.com/v2/login/authorization/dialog"
_TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
_CACHE_DIR = Path("/tmp/agentx_upstox")

# Instrument master is large (~1-2 MB gz). Cache the symbol→instrument_key map
# in memory for a day; the underlying file changes at most once per trading day.
_INSTRUMENT_TTL = 24 * 3600
_instrument_maps: dict[str, dict[str, str]] = {}
_instrument_loaded_at: dict[str, float] = {}

# Map our internal interval tokens to Upstox v2 daily/weekly/monthly buckets.
_INTERVAL_MAP = {"1d": "day", "1wk": "week", "1mo": "month"}

# Upstox v2 intraday supports only 1minute / 30minute buckets. Tokens we can't
# serve natively (5m/15m/1h) return None so the caller cascades to yfinance.
_INTRADAY_MAP = {"1m": "1minute", "30m": "30minute"}

# Underlying instrument keys for the F&O indices (not in the EQ master).
_INDEX_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
    "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
}


def has_token(settings: dict[str, Any]) -> bool:
    """True when a usable Upstox access token is configured."""
    return bool((settings or {}).get("upstox_access_token"))


# ── Rate-limit (HTTP 429) handling ───────────────────────────
# Upstox returns 429 when we exceed its request budget. We back off ONCE for
# the server-advertised window (Retry-After), then return None so the caller's
# source-health cooldown + next scan cycle handle the rest — no retry loop.
_RATELIMIT_DEFAULT = 2.0   # seconds — when Retry-After is missing/unparseable
_RATELIMIT_CAP = 30.0      # seconds — never sleep longer than this


class _RateLimited(Exception):
    """Raised inside a sync request path on HTTP 429 to defer the async sleep.

    Carries the parsed back-off (seconds) so the async wrapper can
    ``await asyncio.sleep`` it without blocking the executor thread.
    """

    def __init__(self, backoff: float) -> None:
        super().__init__(f"rate-limited; backing off {backoff:.0f}s")
        self.backoff = backoff


def _parse_retry_after(value: Optional[str]) -> float:
    """Parse a ``Retry-After`` header (integer seconds) into a capped back-off.

    Only the integer-seconds form is handled; an HTTP-date or unparseable
    value falls back to :data:`_RATELIMIT_DEFAULT`. Result is clamped to
    ``[_RATELIMIT_DEFAULT, _RATELIMIT_CAP]``.
    """
    try:
        secs = float(int((value or "").strip()))
    except (ValueError, TypeError):
        return _RATELIMIT_DEFAULT
    if secs <= 0:
        return _RATELIMIT_DEFAULT
    return min(secs, _RATELIMIT_CAP)


# ── Instrument key resolution ────────────────────────────────

def _load_instrument_map(exchange: str) -> dict[str, str]:
    """Return ``{TRADING_SYMBOL: instrument_key}`` for equities on ``exchange``.

    Downloads and parses the Upstox instrument master once per day, caching the
    parsed JSON on disk so a restart doesn't re-download. Returns an empty dict
    on any failure (caller treats that as "can't resolve" and falls back).
    """
    exch = exchange.upper()
    now = time.time()
    cached = _instrument_maps.get(exch)
    if cached is not None and (now - _instrument_loaded_at.get(exch, 0)) < _INSTRUMENT_TTL:
        return cached

    import requests

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    disk_path = _CACHE_DIR / f"{exch}.json"

    records: list[dict] | None = None
    # Prefer a same-day on-disk copy before hitting the network.
    try:
        if disk_path.exists() and (now - disk_path.stat().st_mtime) < _INSTRUMENT_TTL:
            records = json.loads(disk_path.read_text())
    except Exception as e:  # pragma: no cover - disk corruption is rare
        logger.debug("upstox: cached instrument file unreadable: %s", e)

    if records is None:
        try:
            resp = requests.get(
                _INSTRUMENT_URL.format(exch=exch), timeout=_HTTP_TIMEOUT
            )
            resp.raise_for_status()
            records = json.loads(gzip.decompress(resp.content))
            try:
                disk_path.write_text(json.dumps(records))
            except Exception:  # pragma: no cover - cache write best-effort
                pass
        except Exception as e:
            logger.warning("upstox: instrument master download failed for %s: %s", exch, e)
            return {}

    mapping: dict[str, str] = {}
    for rec in records or []:
        if rec.get("instrument_type") != "EQ":
            continue
        sym = rec.get("trading_symbol")
        key = rec.get("instrument_key")
        if sym and key:
            mapping[sym.upper()] = key

    _instrument_maps[exch] = mapping
    _instrument_loaded_at[exch] = now
    logger.info("upstox: loaded %d %s equity instruments", len(mapping), exch)
    return mapping


def _resolve_instrument_key(symbol: str, exchange: str) -> Optional[str]:
    mapping = _load_instrument_map(exchange)
    return mapping.get(symbol.upper())


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


# ── Historical OHLCV ─────────────────────────────────────────

def _sync_fetch_history(
    symbol: str, days: int, interval: str, token: str, exchange: str,
) -> Optional[pd.DataFrame]:
    import requests

    up_interval = _INTERVAL_MAP.get(interval)
    if up_interval is None:
        return None  # intraday — not our job; let yfinance handle it
    key = _resolve_instrument_key(symbol, exchange)
    if not key:
        logger.debug("upstox: no instrument_key for %s on %s", symbol, exchange)
        return None

    to_date = date.today()
    from_date = to_date - timedelta(days=days)
    # instrument_key contains a pipe; requests encodes it in the path safely.
    url = (
        f"{_BASE}/historical-candle/{key}/{up_interval}"
        f"/{to_date.isoformat()}/{from_date.isoformat()}"
    )
    try:
        resp = requests.get(url, headers=_auth_headers(token), timeout=_HTTP_TIMEOUT)
        if resp.status_code == 401:
            logger.warning("upstox: 401 on history — token lacks market-data access (use an Analytics Token; see Settings)")
            return None
        if resp.status_code == 429:
            raise _RateLimited(_parse_retry_after(resp.headers.get("Retry-After")))
        resp.raise_for_status()
        candles = (resp.json().get("data") or {}).get("candles") or []
    except _RateLimited:
        raise
    except Exception as e:
        logger.debug("upstox history failed for %s: %s", symbol, e)
        return None

    if not candles:
        return None

    records = []
    for c in candles:
        try:
            records.append({
                "Date": pd.Timestamp(c[0]).tz_localize(None),
                "Open": float(c[1]), "High": float(c[2]),
                "Low": float(c[3]), "Close": float(c[4]),
                "Volume": float(c[5]),
            })
        except (IndexError, ValueError, TypeError):
            continue
    if not records:
        return None
    df = pd.DataFrame(records).set_index("Date").sort_index()
    return df


async def upstox_fetch_history(
    symbol: str,
    *,
    days: int = 370,
    interval: str = "1d",
    token: str,
    exchange: str = "NSE",
) -> Optional[pd.DataFrame]:
    """Async daily/weekly/monthly OHLCV via Upstox. ``None`` on any failure."""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(
            None, _sync_fetch_history, symbol, days, interval, token, exchange,
        )
    except _RateLimited as e:
        logger.warning("Upstox rate-limited (429), backing off %ss", int(e.backoff))
        await asyncio.sleep(e.backoff)
        return None


# ── Live quote ───────────────────────────────────────────────

def _sync_fetch_quote(symbol: str, token: str, exchange: str) -> Optional[dict]:
    import requests

    key = _resolve_instrument_key(symbol, exchange)
    if not key:
        return None
    try:
        resp = requests.get(
            f"{_BASE}/market-quote/quotes",
            headers=_auth_headers(token),
            params={"instrument_key": key},
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code == 401:
            logger.warning("upstox: 401 on quote — token lacks market-data access (use an Analytics Token; see Settings)")
            return None
        if resp.status_code == 429:
            raise _RateLimited(_parse_retry_after(resp.headers.get("Retry-After")))
        resp.raise_for_status()
        data = resp.json().get("data") or {}
    except _RateLimited:
        raise
    except Exception as e:
        logger.debug("upstox quote failed for %s: %s", symbol, e)
        return None

    if not data:
        return None
    # Response key is "NSE_EQ:SYMBOL"; take the single entry we asked for.
    payload = next(iter(data.values()), None)
    if not payload or payload.get("last_price") is None:
        return None
    ohlc = payload.get("ohlc") or {}
    last = payload.get("last_price")
    prev_close = ohlc.get("close")
    return {
        "symbol": symbol,
        "lastPrice": last,
        "change": payload.get("net_change"),
        "pChange": (
            round((last - prev_close) / prev_close * 100, 2)
            if prev_close else None
        ),
        "open": ohlc.get("open"),
        "high": ohlc.get("high"),
        "low": ohlc.get("low"),
        "previousClose": prev_close,
        "totalTradedVolume": payload.get("volume"),
        "source": "upstox",
    }


async def upstox_fetch_quote(
    symbol: str, *, token: str, exchange: str = "NSE",
) -> Optional[dict]:
    """Async live quote via Upstox. ``None`` on any failure."""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _sync_fetch_quote, symbol, token, exchange)
    except _RateLimited as e:
        logger.warning("Upstox rate-limited (429), backing off %ss", int(e.backoff))
        await asyncio.sleep(e.backoff)
        return None


# ── Intraday OHLCV ───────────────────────────────────────────

def _sync_fetch_intraday(symbol: str, interval: str, token: str, exchange: str) -> Optional[pd.DataFrame]:
    import requests

    up_interval = _INTRADAY_MAP.get(interval)
    if up_interval is None:
        return None  # 5m/15m/1h not served by Upstox v2 — caller uses yfinance
    key = _resolve_instrument_key(symbol, exchange)
    if not key:
        return None
    url = f"{_BASE}/historical-candle/intraday/{key}/{up_interval}"
    try:
        resp = requests.get(url, headers=_auth_headers(token), timeout=_HTTP_TIMEOUT)
        if resp.status_code == 401:
            logger.warning("upstox: 401 on intraday — token lacks market-data access (use an Analytics Token; see Settings)")
            return None
        if resp.status_code == 429:
            raise _RateLimited(_parse_retry_after(resp.headers.get("Retry-After")))
        resp.raise_for_status()
        candles = (resp.json().get("data") or {}).get("candles") or []
    except _RateLimited:
        raise
    except Exception as e:
        logger.debug("upstox intraday failed for %s: %s", symbol, e)
        return None
    if not candles:
        return None
    records = []
    for c in candles:
        try:
            records.append({
                "Date": pd.Timestamp(c[0]).tz_localize(None),
                "Open": float(c[1]), "High": float(c[2]),
                "Low": float(c[3]), "Close": float(c[4]), "Volume": float(c[5]),
            })
        except (IndexError, ValueError, TypeError):
            continue
    if not records:
        return None
    return pd.DataFrame(records).set_index("Date").sort_index()


async def upstox_fetch_intraday(
    symbol: str, *, interval: str = "1m", token: str, exchange: str = "NSE",
) -> Optional[pd.DataFrame]:
    """Async intraday OHLCV via Upstox (1minute / 30minute only). ``None`` otherwise."""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(
            None, _sync_fetch_intraday, symbol, interval, token, exchange,
        )
    except _RateLimited as e:
        logger.warning("Upstox rate-limited (429), backing off %ss", int(e.backoff))
        await asyncio.sleep(e.backoff)
        return None


# ── Option chain (normalized to the NSE record shape) ────────

def _resolve_underlying_key(symbol: str, exchange: str) -> Optional[str]:
    """Underlying instrument key for an option chain — index or equity."""
    return _INDEX_KEYS.get(symbol.upper()) or _resolve_instrument_key(symbol, exchange)


def _leg_to_nse_shape(strike: float, expiry: str, leg: dict) -> dict:
    """Convert one Upstox call/put leg into the NSE ``CE``/``PE`` sub-dict shape
    so the existing ``market_data`` analyzer can consume it unchanged."""
    md = (leg or {}).get("market_data") or {}
    greeks = (leg or {}).get("option_greeks") or {}
    oi = md.get("oi") or 0
    prev_oi = md.get("prev_oi") or 0
    return {
        "strikePrice": strike,
        "expiryDate": expiry,
        "openInterest": oi,
        "changeinOpenInterest": (oi - prev_oi) if (oi or prev_oi) else 0,
        "totalTradedVolume": md.get("volume") or 0,
        "impliedVolatility": greeks.get("iv"),
        "lastPrice": md.get("ltp"),
    }


def _sync_fetch_option_chain(symbol: str, token: str, exchange: str) -> Optional[dict]:
    """Fetch + normalize the Upstox option chain.

    Returns ``{"strikes": [...NSE-shaped...], "expiry_dates": [expiry],
    "underlying_value": spot}`` or ``None``. Picks the nearest expiry via the
    option/contract endpoint, then pulls that expiry's chain.
    """
    import requests

    key = _resolve_underlying_key(symbol, exchange)
    if not key:
        return None
    headers = _auth_headers(token)
    try:
        # 1. Discover expiries, pick the nearest one that isn't today.
        cresp = requests.get(
            f"{_BASE}/option/contract", headers=headers,
            params={"instrument_key": key}, timeout=_HTTP_TIMEOUT,
        )
        if cresp.status_code == 401:
            logger.warning("upstox: 401 on option/contract — token lacks market-data access (use an Analytics Token; see Settings)")
            return None
        if cresp.status_code == 429:
            raise _RateLimited(_parse_retry_after(cresp.headers.get("Retry-After")))
        cresp.raise_for_status()
        contracts = cresp.json().get("data") or []
        expiries = sorted({c.get("expiry") for c in contracts if c.get("expiry")})
        if not expiries:
            return None
        today_iso = date.today().isoformat()
        expiry = next((e for e in expiries if e > today_iso), expiries[0])

        # 2. Pull the chain for that expiry.
        oresp = requests.get(
            f"{_BASE}/option/chain", headers=headers,
            params={"instrument_key": key, "expiry_date": expiry},
            timeout=_HTTP_TIMEOUT,
        )
        if oresp.status_code == 429:
            raise _RateLimited(_parse_retry_after(oresp.headers.get("Retry-After")))
        oresp.raise_for_status()
        rows = oresp.json().get("data") or []
    except _RateLimited:
        raise
    except Exception as e:
        logger.debug("upstox option chain failed for %s: %s", symbol, e)
        return None

    if not rows:
        return None
    strikes = []
    spot = None
    for row in rows:
        strike = row.get("strike_price")
        if strike is None:
            continue
        spot = spot or row.get("underlying_spot_price")
        strikes.append({
            "strikePrice": strike,
            "expiryDate": expiry,
            "CE": _leg_to_nse_shape(strike, expiry, row.get("call_options")),
            "PE": _leg_to_nse_shape(strike, expiry, row.get("put_options")),
        })
    if not strikes or spot is None:
        return None
    return {"strikes": strikes, "expiry_dates": [expiry], "underlying_value": spot}


async def upstox_fetch_option_chain(
    symbol: str, *, token: str, exchange: str = "NSE",
) -> Optional[dict]:
    """Async normalized option chain. ``None`` on any failure."""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(
            None, _sync_fetch_option_chain, symbol, token, exchange,
        )
    except _RateLimited as e:
        logger.warning("Upstox rate-limited (429), backing off %ss", int(e.backoff))
        await asyncio.sleep(e.backoff)
        return None


# ── OAuth2 token generation (for the Settings UI flow) ───────
# Upstox issues short-lived access tokens via the authorization-code grant:
#   1. Open build_login_url(...) in a browser, log in, approve.
#   2. Upstox redirects to redirect_uri?code=<CODE>.
#   3. exchange_code(<CODE>, ...) trades the code for an access_token.
# The token expires daily (~03:30 IST), so this is re-run each trading day.

def build_login_url(api_key: str, redirect_uri: str, state: str = "agentx") -> str:
    """Return the Upstox login dialog URL to open in a browser.

    ``api_key`` is the Upstox app's API key (client_id). ``redirect_uri`` must
    exactly match the one registered on the Upstox app, or Upstox rejects it.
    """
    from urllib.parse import urlencode

    query = urlencode({
        "response_type": "code",
        "client_id": api_key,
        "redirect_uri": redirect_uri,
        "state": state,
    })
    return f"{_LOGIN_DIALOG}?{query}"


def _sync_exchange_code(
    code: str, api_key: str, api_secret: str, redirect_uri: str,
) -> dict[str, Any]:
    """Trade an authorization code for an access token. Never raises."""
    import requests

    if not (code and api_key and api_secret and redirect_uri):
        return {
            "ok": False,
            "message": "Need code, upstox_api_key, upstox_api_secret and redirect_uri.",
        }
    try:
        resp = requests.post(
            _TOKEN_URL,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "code": code,
                "client_id": api_key,
                "client_secret": api_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=_HTTP_TIMEOUT,
        )
        body = resp.json() if resp.content else {}
    except Exception as e:
        return {"ok": False, "message": f"Token exchange failed: {e}"}

    token = (body or {}).get("access_token")
    if not token:
        # Upstox returns {"errors":[{"message": "..."}]} on failure.
        errs = (body or {}).get("errors") or []
        detail = errs[0].get("message") if errs and isinstance(errs[0], dict) else None
        return {
            "ok": False,
            "message": detail or f"No access_token in response (HTTP {resp.status_code}).",
        }
    return {"ok": True, "access_token": token, "message": "Access token generated."}


async def exchange_code(
    code: str, *, api_key: str, api_secret: str, redirect_uri: str,
) -> dict[str, Any]:
    """Async wrapper for the OAuth code→token exchange."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _sync_exchange_code, code, api_key, api_secret, redirect_uri,
    )


# ── Connection test (for the Settings UI button) ─────────────

_TEST_SYMBOL = "RELIANCE"  # liquid NSE equity always in the instrument master


def _sync_test_connection(token: str) -> dict[str, Any]:
    """Validate a token against the *market-data* path (LTP quote).

    We deliberately do NOT hit ``/v2/user/profile``: this app uses Upstox only
    as a market-data source, and the supported credential for that is the
    long-lived **Analytics Token**, which is market-data-only and returns 401
    on ``/user/profile`` (account endpoint, static-IP restricted). Validating
    against profile therefore gave false negatives for a working data token —
    and false positives for a daily OAuth token that 401s on quotes. Hitting
    LTP confirms the only thing that matters here: is live data flowing.
    """
    import requests

    if not token:
        return {"ok": False, "message": "No Upstox access token configured."}

    key = _resolve_instrument_key(_TEST_SYMBOL, "NSE")
    if not key:
        return {"ok": False, "message": "Could not load Upstox instrument master — check network."}
    try:
        resp = requests.get(
            f"{_BASE}/market-quote/ltp", headers=_auth_headers(token),
            params={"instrument_key": key}, timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code == 401:
            return {
                "ok": False,
                "message": (
                    "Token rejected for market data (401). Use an Upstox "
                    "Analytics Token (Developer Apps → Analytics tab → Generate "
                    "Token) — the daily OAuth access token does not grant "
                    "market-data access."
                ),
            }
        resp.raise_for_status()
        payload = next(iter((resp.json().get("data") or {}).values()), None)
        ltp = (payload or {}).get("last_price")
        if ltp is None:
            return {"ok": False, "message": "Connected, but no price returned (market may be closed)."}
        return {
            "ok": True,
            "message": f"Connected — live market data flowing ({_TEST_SYMBOL} LTP ₹{ltp}).",
        }
    except Exception as e:
        return {"ok": False, "message": f"Connection failed: {e}"}


async def test_connection(token: str) -> dict[str, Any]:
    """Async wrapper for the Settings 'Test connection' button."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_test_connection, token)

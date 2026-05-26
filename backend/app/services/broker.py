"""Unified broker abstraction for AngelOne SmartAPI and Kite Connect.

Goal: agentX should not care which broker the user has connected. Every
downstream consumer (data_fetcher, options analytics, OMS) talks to a
``BrokerClient`` protocol that hides the SDK differences.

This module:
- Defines the ``BrokerClient`` protocol (LTP, OHLC, option chain, place
  order — paper or live).
- Implements ``AngelOneClient`` and ``KiteClient`` adapters.
- Exposes a factory ``get_broker_client(settings)`` that returns the
  right adapter based on the user's ``broker`` setting, lazily importing
  the SDKs so the rest of the codebase doesn't pull them at boot.

Both adapters keep their SDK imports *inside* the methods so a missing
package (e.g. ``smartapi-python`` not installed) doesn't crash the
backend — only the call that needs it. Failures degrade gracefully back
to yfinance via ``data_fetcher``.

Credentials are loaded from the existing settings table and unsealed by
``orchestrator._get_settings`` before reaching here. Never re-read or
log them.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Domain types — kept small so adapters don't leak SDK shapes
# ─────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Quote:
    """Lightweight L1 quote — same shape regardless of broker."""
    symbol: str
    exchange: str
    ltp: float
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None    # previous close
    volume: Optional[int] = None


@dataclass(frozen=True)
class OptionLeg:
    """One strike's call OR put leg in an option chain."""
    strike: float
    instrument_token: Optional[int] = None
    last_price: Optional[float] = None
    iv: Optional[float] = None
    oi: Optional[int] = None
    volume: Optional[int] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    vega: Optional[float] = None
    theta: Optional[float] = None


@dataclass(frozen=True)
class OptionChainStrike:
    """Call + put pair at a single strike."""
    strike: float
    call: Optional[OptionLeg] = None
    put: Optional[OptionLeg] = None


@dataclass(frozen=True)
class OptionChainSnapshot:
    """Whole chain for one expiry."""
    underlying: str
    expiry: str
    spot: float
    strikes: list[OptionChainStrike]


# ─────────────────────────────────────────────────────────────────────────
# Protocol
# ─────────────────────────────────────────────────────────────────────────

class BrokerClient(Protocol):
    """Minimum surface every adapter must implement."""

    name: str
    is_live: bool

    async def login(self) -> bool:
        """Authenticate. Returns True on success."""

    async def get_quote(self, symbol: str, *, exchange: str = "NSE") -> Optional[Quote]:
        """Latest tick / LTP."""

    async def get_option_chain(
        self, underlying: str, *, expiry: Optional[str] = None
    ) -> Optional[OptionChainSnapshot]:
        """Full option chain snapshot. ``expiry=None`` ⇒ nearest expiry."""


# ─────────────────────────────────────────────────────────────────────────
# AngelOne SmartAPI adapter
# ─────────────────────────────────────────────────────────────────────────

class AngelOneClient:
    """SmartAPI adapter. TOTP-based 2FA login → session token → REST calls.

    Required settings keys (any may be ``None`` if user hasn't configured):
    - ``angelone_api_key``
    - ``angelone_client_code`` (user ID, e.g. ``A12345``)
    - ``angelone_mpin`` (numeric MPIN)
    - ``angelone_totp_secret`` (base32 from broker setup page)
    """

    name = "angelone"
    is_live = True

    def __init__(self, settings: dict[str, Any]):
        self._api_key = settings.get("angelone_api_key")
        self._client_code = settings.get("angelone_client_code")
        self._mpin = settings.get("angelone_mpin")
        self._totp_secret = settings.get("angelone_totp_secret")
        self._session: Optional[Any] = None      # SmartConnect instance after login

    def _has_credentials(self) -> bool:
        return all([self._api_key, self._client_code, self._mpin, self._totp_secret])

    async def login(self) -> bool:
        if not self._has_credentials():
            logger.debug("AngelOne credentials incomplete; skipping login")
            return False
        try:
            # Imported lazily so missing pkg doesn't break boot.
            from SmartApi import SmartConnect  # type: ignore
            import pyotp  # type: ignore
        except ImportError as e:
            logger.warning(
                "AngelOne SDK not installed (%s). Install with: "
                "pip install smartapi-python pyotp", e,
            )
            return False
        try:
            client = SmartConnect(api_key=self._api_key)
            totp = pyotp.TOTP(self._totp_secret).now()
            data = client.generateSession(self._client_code, self._mpin, totp)
            if not data or not data.get("status"):
                logger.warning("AngelOne login returned non-ok: %s", data)
                return False
            self._session = client
            return True
        except Exception as e:
            logger.warning("AngelOne login failed: %s", e)
            return False

    async def get_quote(self, symbol: str, *, exchange: str = "NSE") -> Optional[Quote]:
        if self._session is None and not await self.login():
            return None
        try:
            token = await self._resolve_token(symbol, exchange)
            if not token:
                return None
            # ltpData(exchange, tradingsymbol, symboltoken)
            data = self._session.ltpData(exchange, symbol, token)
            payload = (data or {}).get("data") or {}
            ltp = payload.get("ltp")
            if ltp is None:
                return None
            return Quote(
                symbol=symbol, exchange=exchange,
                ltp=float(ltp),
                open=_safe_float(payload.get("open")),
                high=_safe_float(payload.get("high")),
                low=_safe_float(payload.get("low")),
                close=_safe_float(payload.get("close")),
            )
        except Exception as e:
            logger.warning("AngelOne ltpData failed for %s: %s", symbol, e)
            return None

    async def get_option_chain(
        self, underlying: str, *, expiry: Optional[str] = None
    ) -> Optional[OptionChainSnapshot]:
        if self._session is None and not await self.login():
            return None
        try:
            # SmartAPI doesn't expose a single "give me the chain" endpoint
            # publicly; the documented pattern is `optionGreek` for greeks
            # by symbol+expiry. We surface what's available; callers can
            # cross-merge with NSE data if needed.
            params = {"name": underlying, "expirydate": expiry} if expiry else {"name": underlying}
            res = self._session.optionGreek(params)   # type: ignore[attr-defined]
            payload = (res or {}).get("data") or []
            strikes: dict[float, dict] = {}
            for row in payload:
                strike = float(row.get("strikePrice") or 0)
                if strike <= 0:
                    continue
                option_type = (row.get("optionType") or "").upper()  # CE / PE
                leg = OptionLeg(
                    strike=strike,
                    last_price=_safe_float(row.get("lastPrice")),
                    iv=_safe_float(row.get("impliedVolatility")),
                    oi=_safe_int(row.get("openInterest")),
                    volume=_safe_int(row.get("tradeVolume")),
                    delta=_safe_float(row.get("delta")),
                    gamma=_safe_float(row.get("gamma")),
                    vega=_safe_float(row.get("vega")),
                    theta=_safe_float(row.get("theta")),
                )
                row_bucket = strikes.setdefault(strike, {"strike": strike, "call": None, "put": None})
                if option_type == "CE":
                    row_bucket["call"] = leg
                elif option_type == "PE":
                    row_bucket["put"] = leg

            spot_quote = await self.get_quote(underlying)
            spot = spot_quote.ltp if spot_quote else 0.0
            return OptionChainSnapshot(
                underlying=underlying,
                expiry=expiry or "",
                spot=spot,
                strikes=[
                    OptionChainStrike(strike=k, call=v["call"], put=v["put"])
                    for k, v in sorted(strikes.items())
                ],
            )
        except Exception as e:
            logger.warning("AngelOne option chain failed for %s: %s", underlying, e)
            return None

    async def _resolve_token(self, symbol: str, exchange: str) -> Optional[str]:
        """Resolve tradingsymbol → symboltoken (cached per-process)."""
        cache = _angelone_token_cache
        key = (exchange, symbol)
        if key in cache:
            return cache[key]
        try:
            results = self._session.searchScrip(exchange=exchange, searchtext=symbol)  # type: ignore[attr-defined]
            for row in (results or {}).get("data") or []:
                if row.get("tradingsymbol", "").upper() == symbol.upper():
                    token = row.get("symboltoken")
                    cache[key] = token
                    return token
        except Exception as e:
            logger.debug("AngelOne searchScrip failed: %s", e)
        return None


_angelone_token_cache: dict[tuple[str, str], str] = {}


# ─────────────────────────────────────────────────────────────────────────
# Kite Connect adapter
# ─────────────────────────────────────────────────────────────────────────

class KiteClient:
    """Zerodha Kite Connect adapter.

    Kite uses a 2-step OAuth: the user logs in via Zerodha's web flow,
    we receive a ``request_token``, exchange it for an ``access_token``
    that lives until ~6 AM next day. The access token is stored as a
    sealed setting (``kite_access_token``).

    Required settings keys:
    - ``kite_api_key``
    - ``kite_api_secret``
    - ``kite_access_token`` (refreshed daily by the user via Settings UI)
    """

    name = "kite"
    is_live = True

    def __init__(self, settings: dict[str, Any]):
        self._api_key = settings.get("kite_api_key")
        self._api_secret = settings.get("kite_api_secret")
        self._access_token = settings.get("kite_access_token")
        self._kite: Optional[Any] = None

    def _has_credentials(self) -> bool:
        return all([self._api_key, self._access_token])

    async def login(self) -> bool:
        if not self._has_credentials():
            logger.debug("Kite credentials incomplete; skipping login")
            return False
        try:
            from kiteconnect import KiteConnect  # type: ignore
        except ImportError as e:
            logger.warning(
                "Kite SDK not installed (%s). Install with: pip install kiteconnect", e,
            )
            return False
        try:
            kite = KiteConnect(api_key=self._api_key)
            kite.set_access_token(self._access_token)
            # Sanity: fetching profile validates the token cheaply.
            kite.profile()
            self._kite = kite
            return True
        except Exception as e:
            logger.warning("Kite login failed (token may have expired): %s", e)
            return False

    async def get_quote(self, symbol: str, *, exchange: str = "NSE") -> Optional[Quote]:
        if self._kite is None and not await self.login():
            return None
        try:
            key = f"{exchange}:{symbol}"
            data = self._kite.ltp([key])
            row = data.get(key) if data else None
            if not row:
                return None
            ltp = row.get("last_price")
            if ltp is None:
                return None
            ohlc = row.get("ohlc") or {}
            return Quote(
                symbol=symbol, exchange=exchange, ltp=float(ltp),
                open=_safe_float(ohlc.get("open")),
                high=_safe_float(ohlc.get("high")),
                low=_safe_float(ohlc.get("low")),
                close=_safe_float(ohlc.get("close")),
            )
        except Exception as e:
            logger.warning("Kite ltp failed for %s: %s", symbol, e)
            return None

    async def get_option_chain(
        self, underlying: str, *, expiry: Optional[str] = None
    ) -> Optional[OptionChainSnapshot]:
        if self._kite is None and not await self.login():
            return None
        try:
            # Kite exposes the full instrument master; we filter for the
            # nearest expiry of the underlying and build the chain.
            from datetime import date as _date
            instruments = self._kite.instruments("NFO")
            matches = [
                i for i in instruments
                if i.get("name") == underlying.upper() and i.get("segment") == "NFO-OPT"
            ]
            if not matches:
                return None
            if expiry:
                matches = [i for i in matches if str(i.get("expiry")) == expiry]
            else:
                # Pick nearest expiry ≥ today.
                today = _date.today()
                future = [i for i in matches if i.get("expiry") and i["expiry"] >= today]
                future.sort(key=lambda i: i["expiry"])
                target_expiry = future[0]["expiry"] if future else None
                matches = [i for i in matches if i.get("expiry") == target_expiry]

            # Pull quotes for the strikes in one shot (Kite supports batches).
            keys = [f"NFO:{i['tradingsymbol']}" for i in matches]
            quote_data = self._kite.quote(keys) if keys else {}
            strikes: dict[float, dict] = {}
            for inst in matches:
                strike = float(inst["strike"])
                q = quote_data.get(f"NFO:{inst['tradingsymbol']}") or {}
                leg = OptionLeg(
                    strike=strike,
                    instrument_token=inst.get("instrument_token"),
                    last_price=_safe_float(q.get("last_price")),
                    oi=_safe_int(q.get("oi")),
                    volume=_safe_int(q.get("volume")),
                    # IV/Greeks NOT exposed by Kite — caller must compute via options_greeks.
                )
                row = strikes.setdefault(strike, {"strike": strike, "call": None, "put": None})
                if inst.get("instrument_type") == "CE":
                    row["call"] = leg
                elif inst.get("instrument_type") == "PE":
                    row["put"] = leg

            spot_quote = await self.get_quote(underlying)
            spot = spot_quote.ltp if spot_quote else 0.0
            expiry_label = str(matches[0]["expiry"]) if matches else (expiry or "")
            return OptionChainSnapshot(
                underlying=underlying, expiry=expiry_label, spot=spot,
                strikes=[
                    OptionChainStrike(strike=k, call=v["call"], put=v["put"])
                    for k, v in sorted(strikes.items())
                ],
            )
        except Exception as e:
            logger.warning("Kite option chain failed for %s: %s", underlying, e)
            return None


# ─────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────

def get_broker_client(settings: dict[str, Any]) -> Optional[BrokerClient]:
    """Return the broker client the user has configured.

    ``settings['broker']`` ∈ {"angelone", "kite", "" / None}. If unset or
    no credentials are present, returns ``None`` and the caller should
    fall back to yfinance via ``data_fetcher``.
    """
    choice = (settings.get("broker") or "").strip().lower()
    if choice == "angelone":
        client = AngelOneClient(settings)
        return client if client._has_credentials() else None
    if choice == "kite":
        client = KiteClient(settings)
        return client if client._has_credentials() else None
    return None


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        return int(v)
    except (TypeError, ValueError):
        return None

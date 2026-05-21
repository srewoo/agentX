from __future__ import annotations
"""
Market data fetching — NseIndiaApi as PRIMARY, yfinance as FALLBACK.

Data source priority:
  1. NseIndiaApi (pip install nse[local]) — direct NSE endpoints, 3 req/sec, free
  2. yfinance — unofficial Yahoo Finance scraper, rate-limited, 15min delay

Architecture: All public functions are async. Sync NSE/yfinance calls run in
thread executors. The orchestrator and routers call these functions.
"""
import asyncio
import logging
import random
import warnings
from typing import Any
import pandas as pd
import yfinance as yf

from app.services.nse_fetcher import (
    nse_fetch_quote,
    nse_fetch_history,
    nse_fetch_ohlcv,
    nse_fetch_indices,
)

logger = logging.getLogger(__name__)

# ── Retry helper ──────────────────────────────────────────────

async def _retry_async(fn, *args, max_retries: int = 2, base_delay: float = 1.0, **kwargs):
    """Retry an async function with exponential backoff + jitter."""
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                logger.warning(
                    "Retry %d/%d for %s: %s. Waiting %.1fs",
                    attempt + 1, max_retries, fn.__name__, e, delay,
                )
                await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


# ── Period string → days mapping ──────────────────────────────

_PERIOD_TO_DAYS = {
    "5d": 7,
    "1mo": 35,
    "3mo": 100,
    "6mo": 200,
    "1y": 370,
    "2y": 740,
    "5y": 1850,
}


# ── yfinance (fallback) ──────────────────────────────────────

def _yfinance_fetch_sync(
    symbol: str,
    period: str = "6mo",
    interval: str = "1d",
    exchange: str = "NSE",
) -> pd.DataFrame:
    """Sync yfinance fetch. Tries the requested exchange suffix first.

    `exchange="BSE"` flips the lookup order to `.BO` then `.NS` — important
    when the user explicitly picks BSE in the header toggle, since some
    stocks (smaller listings, SME segment) are BSE-only.
    """
    if symbol.startswith("^") or symbol.endswith(".NS") or symbol.endswith(".BO") or "=" in symbol:
        candidates = [symbol]
    elif exchange.upper() == "BSE":
        candidates = [f"{symbol}.BO", f"{symbol}.NS"]
    else:
        candidates = [f"{symbol}.NS", f"{symbol}.BO"]

    for yf_sym in candidates:
        try:
            hist = yf.Ticker(yf_sym).history(period=period, interval=interval)
            if not hist.empty:
                return hist
        except Exception as e:
            logger.debug("yfinance attempt for %s: %s", yf_sym, e)

    return pd.DataFrame()


_YFINANCE_TIMEOUT = 30  # seconds — prevent yfinance from hanging indefinitely
_YFINANCE_INFO_TIMEOUT = 8  # seconds — `.info` is metadata only; fail fast


async def _yfinance_fetch(
    symbol: str, period: str, interval: str, exchange: str = "NSE"
) -> pd.DataFrame:
    """Async wrapper for yfinance with timeout protection."""
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(
                None, _yfinance_fetch_sync, symbol, period, interval, exchange
            ),
            timeout=_YFINANCE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("yfinance fetch timed out after %ds for %s", _YFINANCE_TIMEOUT, symbol)
        return pd.DataFrame()


# ── Main fetch function: NSE first, yfinance fallback ─────────

async def async_fetch_history(
    symbol: str,
    period: str = "6mo",
    interval: str = "1d",
    exchange: str = "NSE",
) -> pd.DataFrame:
    """
    Fetch OHLCV history. NSE primary, yfinance fallback.

    For daily data: tries NseIndiaApi historical endpoint first.
    For intraday data: goes straight to yfinance (NSE doesn't provide intraday OHLCV).
    """
    clean_symbol = symbol.replace(".NS", "").replace(".BO", "")
    is_index = symbol.startswith("^")
    is_intraday = interval not in ("1d", "1wk", "1mo")

    # When the caller explicitly picked BSE we skip the NSE-only primary
    # path entirely — NseIndiaApi has no BSE coverage. Go straight to
    # yfinance with the .BO suffix preferred.
    bse_only = exchange.upper() == "BSE" and not is_index

    # Intraday or index data — NSE does not provide this endpoint here.
    if is_intraday or is_index or bse_only:
        try:
            hist = await _retry_async(
                _yfinance_fetch, symbol, period, interval, exchange, max_retries=1
            )
            if not hist.empty:
                return hist
        except Exception as e:
            logger.debug("yfinance failed for %s: %s", symbol, e)
        return pd.DataFrame()

    # Daily data — try NSE first (faster, no rate limit issues)
    days = _PERIOD_TO_DAYS.get(period, 370)
    try:
        nse_df = await nse_fetch_history(clean_symbol, days=days)
        if nse_df is not None and not nse_df.empty and len(nse_df) >= 5:
            return nse_df
    except Exception as e:
        logger.debug("NSE historical failed for %s: %s", clean_symbol, e)

    # Fallback to yfinance
    try:
        hist = await _retry_async(
            _yfinance_fetch, symbol, period, interval, exchange, max_retries=1
        )
        if not hist.empty:
            return hist
    except Exception as e:
        logger.debug("yfinance fallback failed for %s: %s", symbol, e)

    return pd.DataFrame()


# ── Stock info ────────────────────────────────────────────────

def _stock_info_from_major(symbol: str) -> dict[str, Any] | None:
    """Return cached metadata from MAJOR_STOCKS, or None if unknown."""
    for s in MAJOR_STOCKS:
        if s["symbol"] == symbol:
            return {
                "name": s["name"],
                "sector": s.get("sector", "N/A"),
                "industry": "N/A",
                "pe_ratio": None,
                "market_cap": None,
                "currency": "INR",
            }
    return None


def _yfinance_info_sync(symbol: str) -> dict[str, Any]:
    """Sync yfinance `.info` lookup. Returns normalized dict; raises on failure."""
    yf_sym = symbol if (symbol.startswith("^") or "." in symbol) else f"{symbol}.NS"
    info = yf.Ticker(yf_sym).info
    return {
        "name": info.get("longName", symbol),
        "sector": info.get("sector", "N/A"),
        "industry": info.get("industry", "N/A"),
        "pe_ratio": info.get("trailingPE"),
        "market_cap": info.get("marketCap"),
        "currency": info.get("currency", "INR"),
    }


def get_stock_info(symbol: str) -> dict[str, Any]:
    """Fetch stock metadata. DEPRECATED — sync; blocks the event loop.

    Use :func:`get_stock_info_async` from any async code path. This sync
    variant is retained only for callers that have not yet migrated.
    """
    warnings.warn(
        "get_stock_info is sync and blocks the event loop; use get_stock_info_async",
        DeprecationWarning,
        stacklevel=2,
    )
    cached = _stock_info_from_major(symbol)
    if cached is not None:
        return cached

    try:
        return _yfinance_info_sync(symbol)
    except Exception as e:
        logger.debug("Could not fetch info for %s: %s", symbol, e)
        return {"name": symbol, "sector": "N/A"}


async def get_stock_info_async(
    symbol: str,
    *,
    timeout: float = _YFINANCE_INFO_TIMEOUT,
) -> dict[str, Any]:
    """Async stock metadata fetch. NSE-cached entries first, yfinance fallback.

    The yfinance call is offloaded to the default executor and bounded by
    ``timeout`` seconds (default :data:`_YFINANCE_INFO_TIMEOUT`). On timeout
    or upstream error a minimal stub ``{"name": symbol, "sector": "N/A"}`` is
    returned so callers do not need to special-case failures.

    Args:
        symbol: NSE/BSE ticker (with or without ``.NS`` suffix) or index symbol.
        timeout: Hard upper bound on the executor call, seconds.

    Returns:
        Normalized metadata dict — keys: name, sector, industry, pe_ratio,
        market_cap, currency.
    """
    cached = _stock_info_from_major(symbol)
    if cached is not None:
        return cached

    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _yfinance_info_sync, symbol),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "yfinance .info timed out after %.1fs for %s", timeout, symbol,
        )
        return {"name": symbol, "sector": "N/A"}
    except Exception as e:
        logger.debug("Could not fetch info for %s: %s", symbol, e)
        return {"name": symbol, "sector": "N/A"}


async def get_stock_quote(symbol: str) -> dict[str, Any]:
    """Fetch a live stock quote. NSE first, yfinance fallback."""
    clean_symbol = symbol.replace(".NS", "").replace(".BO", "")

    # Try NSE (fast, reliable)
    try:
        nse_quote = await nse_fetch_quote(clean_symbol)
        if nse_quote and nse_quote.get("lastPrice"):
            return nse_quote
    except Exception as e:
        logger.debug("NSE quote failed for %s: %s", clean_symbol, e)

    # yfinance fallback
    try:
        yf_sym = symbol if (symbol.startswith("^") or "." in symbol) else f"{symbol}.NS"
        loop = asyncio.get_event_loop()
        info = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: yf.Ticker(yf_sym).info),
            timeout=_YFINANCE_TIMEOUT,
        )
        if info and info.get("regularMarketPrice"):
            return {
                "symbol": symbol,
                "lastPrice": info.get("regularMarketPrice"),
                "change": info.get("regularMarketChange"),
                "pChange": info.get("regularMarketChangePercent"),
                "open": info.get("regularMarketOpen"),
                "previousClose": info.get("regularMarketPreviousClose"),
                "high": info.get("regularMarketDayHigh"),
                "low": info.get("regularMarketDayLow"),
                "totalTradedVolume": info.get("regularMarketVolume"),
                "source": "yfinance",
            }
    except Exception as e:
        logger.debug("yfinance quote failed for %s: %s", symbol, e)

    return {"symbol": symbol, "lastPrice": None, "error": "All data sources failed"}


# ── Delivery Volume % ────────────────────────────────────────
# NSE bhavcopy contains delivery data — the fraction of volume that was
# actual buying/selling (not intraday speculation).
# >60% delivery = institutional accumulation, <30% = speculative noise.

async def get_delivery_volume(symbol: str) -> dict[str, Any]:
    """Fetch delivery volume % from NSE for a symbol.

    Returns:
        {
            "symbol": str,
            "delivery_pct": float or None,   # % of traded volume delivered
            "traded_qty": int or None,
            "delivered_qty": int or None,
            "source": str,
        }
    """
    clean = symbol.replace(".NS", "").replace(".BO", "")

    # Try NSE delivery data first
    try:
        result = await _fetch_nse_delivery(clean)
        if result and result.get("delivery_pct") is not None:
            return result
    except Exception as e:
        logger.debug("NSE delivery fetch failed for %s: %s", clean, e)

    return {"symbol": symbol, "delivery_pct": None, "traded_qty": None,
            "delivered_qty": None, "source": "unavailable"}


_NSE_DELIVERY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/get-quotes/equity",
}
_NSE_HOMEPAGE = "https://www.nseindia.com"
_NSE_DELIVERY_TIMEOUT = 12.0
# Module-level session reused across calls so cookies persist between fetches.
_nse_delivery_session: Any = None


def _get_nse_delivery_session() -> Any:
    """Return a shared `requests.Session` for the delivery endpoint.

    Sessions are reused so NSE cookies (set on the homepage warm-up) survive
    between calls. Created lazily to avoid importing `requests` at import time.
    """
    global _nse_delivery_session
    if _nse_delivery_session is None:
        import requests

        _nse_delivery_session = requests.Session()
        _nse_delivery_session.headers.update(_NSE_DELIVERY_HEADERS)
    return _nse_delivery_session


def _warm_nse_session(session: Any) -> None:
    """Hit the NSE homepage so the session picks up the anti-bot cookies."""
    try:
        session.get(_NSE_HOMEPAGE, timeout=_NSE_DELIVERY_TIMEOUT)
    except Exception as e:  # pragma: no cover - logged for diagnostics
        logger.debug("NSE session warm-up failed: %s", e)


async def _fetch_nse_delivery(symbol: str) -> dict[str, Any] | None:
    """Fetch delivery data from NSE API.

    Uses a shared `requests.Session` warmed against the NSE homepage so anti-
    bot cookies persist between calls. On a 403 (cookies expired / IP throttle
    suspected) the session is re-warmed once and the request is retried.
    Returns ``None`` only after both attempts fail.
    """
    import json

    import requests

    loop = asyncio.get_event_loop()

    def _sync_fetch() -> dict[str, Any] | None:
        session = _get_nse_delivery_session()
        url = (
            f"https://www.nseindia.com/api/quote-equity"
            f"?symbol={symbol}&section=trade_info"
        )

        def _do_request() -> requests.Response:
            return session.get(url, timeout=_NSE_DELIVERY_TIMEOUT)

        # First attempt — may 403 if cookies are stale.
        try:
            resp = _do_request()
        except requests.RequestException as e:
            logger.debug("NSE delivery request error for %s: %s", symbol, e)
            return None

        if resp.status_code == 403:
            logger.info(
                "NSE delivery 403 for %s; re-warming session and retrying once",
                symbol,
            )
            _warm_nse_session(session)
            try:
                resp = _do_request()
            except requests.RequestException as e:
                logger.warning(
                    "NSE delivery retry request error for %s: %s", symbol, e,
                )
                return None
            if resp.status_code == 403:
                logger.warning(
                    "NSE delivery 403 for %s after session warm-up; giving up",
                    symbol,
                )
                return None

        if resp.status_code != 200:
            logger.debug(
                "NSE delivery non-200 for %s: %s", symbol, resp.status_code,
            )
            return None

        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError) as e:
            logger.debug("NSE delivery JSON parse failed for %s: %s", symbol, e)
            return None

        # NSE trade_info has securityWiseDP which contains delivery data
        sec_dp = data.get("securityWiseDP", {})
        if not sec_dp:
            # Try marketDeptOrderBook path
            market_data = data.get("marketDeptOrderBook", {})
            trade_info = market_data.get("tradeInfo", {})
            traded_qty = trade_info.get("totalTradedVolume")
            delivered_qty = trade_info.get("deliveryQuantity")
            delivery_pct = trade_info.get("deliveryToTradedQuantity")

            if delivery_pct is not None:
                try:
                    return {
                        "symbol": symbol,
                        "delivery_pct": float(str(delivery_pct).replace(",", "")),
                        "traded_qty": int(float(str(traded_qty).replace(",", ""))) if traded_qty else None,
                        "delivered_qty": int(float(str(delivered_qty).replace(",", ""))) if delivered_qty else None,
                        "source": "nse",
                    }
                except (ValueError, TypeError):
                    pass
            return None

        # Parse securityWiseDP
        delivery_pct_val = sec_dp.get("deliveryToTradedQuantity") or sec_dp.get("delToTradeQty")
        traded_qty = sec_dp.get("totalTradedVolume") or sec_dp.get("quantityTraded")
        delivered_qty = sec_dp.get("deliveryQuantity") or sec_dp.get("deliverableQty")

        if delivery_pct_val is not None:
            try:
                return {
                    "symbol": symbol,
                    "delivery_pct": float(str(delivery_pct_val).replace(",", "").replace("%", "")),
                    "traded_qty": int(float(str(traded_qty).replace(",", ""))) if traded_qty else None,
                    "delivered_qty": int(float(str(delivered_qty).replace(",", ""))) if delivered_qty else None,
                    "source": "nse",
                }
            except (ValueError, TypeError):
                pass

        return None

    # Two HTTP attempts (warm-up + retry) plus the warm-up GET — be generous.
    overall_timeout = (_NSE_DELIVERY_TIMEOUT * 2) + _NSE_DELIVERY_TIMEOUT
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _sync_fetch),
            timeout=overall_timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("NSE delivery fetch timed out for %s", symbol)
        return None
    except Exception as e:
        logger.debug("NSE delivery fetch error for %s: %s", symbol, e)
        return None


# Major Indian stocks for scanning (NIFTY 50 + broader universe)
MAJOR_STOCKS = [
    # NIFTY 50
    {"symbol": "RELIANCE", "name": "Reliance Industries", "sector": "Energy"},
    {"symbol": "TCS", "name": "Tata Consultancy Services", "sector": "IT"},
    {"symbol": "HDFCBANK", "name": "HDFC Bank", "sector": "Banking"},
    {"symbol": "INFY", "name": "Infosys", "sector": "IT"},
    {"symbol": "ICICIBANK", "name": "ICICI Bank", "sector": "Banking"},
    {"symbol": "HINDUNILVR", "name": "Hindustan Unilever", "sector": "FMCG"},
    {"symbol": "SBIN", "name": "State Bank of India", "sector": "Banking"},
    {"symbol": "BHARTIARTL", "name": "Bharti Airtel", "sector": "Telecom"},
    {"symbol": "ITC", "name": "ITC Limited", "sector": "FMCG"},
    {"symbol": "KOTAKBANK", "name": "Kotak Mahindra Bank", "sector": "Banking"},
    {"symbol": "LT", "name": "Larsen & Toubro", "sector": "Infrastructure"},
    {"symbol": "AXISBANK", "name": "Axis Bank", "sector": "Banking"},
    {"symbol": "WIPRO", "name": "Wipro", "sector": "IT"},
    {"symbol": "ASIANPAINT", "name": "Asian Paints", "sector": "Consumer"},
    {"symbol": "MARUTI", "name": "Maruti Suzuki", "sector": "Auto"},
    {"symbol": "TATAMOTORS", "name": "Tata Motors", "sector": "Auto"},
    {"symbol": "SUNPHARMA", "name": "Sun Pharmaceutical", "sector": "Pharma"},
    {"symbol": "BAJFINANCE", "name": "Bajaj Finance", "sector": "Finance"},
    {"symbol": "TITAN", "name": "Titan Company", "sector": "Consumer"},
    {"symbol": "NESTLEIND", "name": "Nestle India", "sector": "FMCG"},
    {"symbol": "TECHM", "name": "Tech Mahindra", "sector": "IT"},
    {"symbol": "HCLTECH", "name": "HCL Technologies", "sector": "IT"},
    {"symbol": "ULTRACEMCO", "name": "UltraTech Cement", "sector": "Cement"},
    {"symbol": "POWERGRID", "name": "Power Grid Corporation", "sector": "Power"},
    {"symbol": "NTPC", "name": "NTPC Limited", "sector": "Power"},
    {"symbol": "ONGC", "name": "Oil & Natural Gas Corp", "sector": "Energy"},
    {"symbol": "TATASTEEL", "name": "Tata Steel", "sector": "Metals"},
    {"symbol": "JSWSTEEL", "name": "JSW Steel", "sector": "Metals"},
    {"symbol": "ADANIENT", "name": "Adani Enterprises", "sector": "Conglomerate"},
    {"symbol": "ADANIPORTS", "name": "Adani Ports", "sector": "Infrastructure"},
    {"symbol": "COALINDIA", "name": "Coal India", "sector": "Mining"},
    {"symbol": "DRREDDY", "name": "Dr Reddys Laboratories", "sector": "Pharma"},
    {"symbol": "CIPLA", "name": "Cipla", "sector": "Pharma"},
    {"symbol": "EICHERMOT", "name": "Eicher Motors", "sector": "Auto"},
    {"symbol": "HEROMOTOCO", "name": "Hero MotoCorp", "sector": "Auto"},
    {"symbol": "BAJAJFINSV", "name": "Bajaj Finserv", "sector": "Finance"},
    {"symbol": "BRITANNIA", "name": "Britannia Industries", "sector": "FMCG"},
    {"symbol": "DIVISLAB", "name": "Divis Laboratories", "sector": "Pharma"},
    {"symbol": "GRASIM", "name": "Grasim Industries", "sector": "Cement"},
    {"symbol": "APOLLOHOSP", "name": "Apollo Hospitals", "sector": "Healthcare"},
    {"symbol": "HDFCLIFE", "name": "HDFC Life Insurance", "sector": "Insurance"},
    {"symbol": "SBILIFE", "name": "SBI Life Insurance", "sector": "Insurance"},
    {"symbol": "TATACONSUM", "name": "Tata Consumer Products", "sector": "FMCG"},
    {"symbol": "INDUSINDBK", "name": "IndusInd Bank", "sector": "Banking"},
    {"symbol": "HINDALCO", "name": "Hindalco Industries", "sector": "Metals"},
    {"symbol": "BPCL", "name": "Bharat Petroleum", "sector": "Energy"},
    {"symbol": "ZOMATO", "name": "Zomato", "sector": "Consumer"},
    {"symbol": "TRENT", "name": "Trent", "sector": "Consumer"},
    {"symbol": "BEL", "name": "Bharat Electronics", "sector": "Defense"},
    {"symbol": "HAL", "name": "Hindustan Aeronautics", "sector": "Defense"},
    # PSU Banks
    {"symbol": "PNB", "name": "Punjab National Bank", "sector": "Banking"},
    {"symbol": "BANKBARODA", "name": "Bank of Baroda", "sector": "Banking"},
    {"symbol": "CANBK", "name": "Canara Bank", "sector": "Banking"},
    {"symbol": "UNIONBANK", "name": "Union Bank of India", "sector": "Banking"},
    {"symbol": "INDIANB", "name": "Indian Bank", "sector": "Banking"},
    {"symbol": "BANKINDIA", "name": "Bank of India", "sector": "Banking"},
    {"symbol": "IOB", "name": "Indian Overseas Bank", "sector": "Banking"},
    {"symbol": "CENTRALBK", "name": "Central Bank of India", "sector": "Banking"},
    {"symbol": "MAHABANK", "name": "Bank of Maharashtra", "sector": "Banking"},
    {"symbol": "PSB", "name": "Punjab & Sind Bank", "sector": "Banking"},
    # Private Banks
    {"symbol": "FEDERALBNK", "name": "Federal Bank", "sector": "Banking"},
    {"symbol": "BANDHANBNK", "name": "Bandhan Bank", "sector": "Banking"},
    {"symbol": "IDFCFIRSTB", "name": "IDFC First Bank", "sector": "Banking"},
    {"symbol": "RBLBANK", "name": "RBL Bank", "sector": "Banking"},
    {"symbol": "YESBANK", "name": "Yes Bank", "sector": "Banking"},
    {"symbol": "KARURVYSYA", "name": "Karur Vysya Bank", "sector": "Banking"},
    {"symbol": "CSBBANK", "name": "CSB Bank", "sector": "Banking"},
    # IT / Tech
    {"symbol": "MPHASIS", "name": "Mphasis", "sector": "IT"},
    {"symbol": "LTIM", "name": "LTIMindtree", "sector": "IT"},
    {"symbol": "PERSISTENT", "name": "Persistent Systems", "sector": "IT"},
    {"symbol": "COFORGE", "name": "Coforge", "sector": "IT"},
    {"symbol": "OFSS", "name": "Oracle Financial Services", "sector": "IT"},
    {"symbol": "KPITTECH", "name": "KPIT Technologies", "sector": "IT"},
    {"symbol": "TATAELXSI", "name": "Tata Elxsi", "sector": "IT"},
    {"symbol": "CYIENT", "name": "Cyient", "sector": "IT"},
    {"symbol": "MASTEK", "name": "Mastek", "sector": "IT"},
    # Auto & Auto Ancillary
    {"symbol": "BAJAJ-AUTO", "name": "Bajaj Auto", "sector": "Auto"},
    {"symbol": "TVSMOTOR", "name": "TVS Motor Company", "sector": "Auto"},
    {"symbol": "ASHOKLEY", "name": "Ashok Leyland", "sector": "Auto"},
    {"symbol": "M&M", "name": "Mahindra & Mahindra", "sector": "Auto"},
    {"symbol": "BOSCHLTD", "name": "Bosch", "sector": "Auto Ancillary"},
    {"symbol": "MOTHERSON", "name": "Samvardhana Motherson", "sector": "Auto Ancillary"},
    {"symbol": "BALKRISIND", "name": "Balkrishna Industries", "sector": "Auto Ancillary"},
    {"symbol": "MRF", "name": "MRF", "sector": "Auto Ancillary"},
    {"symbol": "APOLLOTYRE", "name": "Apollo Tyres", "sector": "Auto Ancillary"},
    # Pharma / Healthcare
    {"symbol": "LUPIN", "name": "Lupin", "sector": "Pharma"},
    {"symbol": "AUROPHARMA", "name": "Aurobindo Pharma", "sector": "Pharma"},
    {"symbol": "TORNTPHARM", "name": "Torrent Pharmaceuticals", "sector": "Pharma"},
    {"symbol": "ALKEM", "name": "Alkem Laboratories", "sector": "Pharma"},
    {"symbol": "GLENMARK", "name": "Glenmark Pharmaceuticals", "sector": "Pharma"},
    {"symbol": "ABBOTINDIA", "name": "Abbott India", "sector": "Pharma"},
    {"symbol": "IPCA", "name": "IPCA Laboratories", "sector": "Pharma"},
    {"symbol": "MAXHEALTH", "name": "Max Healthcare", "sector": "Healthcare"},
    {"symbol": "FORTIS", "name": "Fortis Healthcare", "sector": "Healthcare"},
    {"symbol": "METROPOLIS", "name": "Metropolis Healthcare", "sector": "Healthcare"},
    # FMCG / Consumer
    {"symbol": "DABUR", "name": "Dabur India", "sector": "FMCG"},
    {"symbol": "MARICO", "name": "Marico", "sector": "FMCG"},
    {"symbol": "COLPAL", "name": "Colgate Palmolive India", "sector": "FMCG"},
    {"symbol": "GODREJCP", "name": "Godrej Consumer Products", "sector": "FMCG"},
    {"symbol": "EMAMILTD", "name": "Emami", "sector": "FMCG"},
    {"symbol": "PGHH", "name": "Procter & Gamble Hygiene", "sector": "FMCG"},
    # Energy / Oil & Gas
    {"symbol": "IOC", "name": "Indian Oil Corporation", "sector": "Energy"},
    {"symbol": "HINDPETRO", "name": "HPCL", "sector": "Energy"},
    {"symbol": "GAIL", "name": "GAIL India", "sector": "Energy"},
    {"symbol": "PETRONET", "name": "Petronet LNG", "sector": "Energy"},
    {"symbol": "OIL", "name": "Oil India", "sector": "Energy"},
    {"symbol": "IGL", "name": "Indraprastha Gas", "sector": "Energy"},
    {"symbol": "MGL", "name": "Mahanagar Gas", "sector": "Energy"},
    # Infrastructure / Construction
    {"symbol": "LTTS", "name": "L&T Technology Services", "sector": "IT"},
    {"symbol": "LICI", "name": "Life Insurance Corporation", "sector": "Insurance"},
    {"symbol": "IRFC", "name": "Indian Railway Finance Corp", "sector": "Finance"},
    {"symbol": "RVNL", "name": "Rail Vikas Nigam", "sector": "Infrastructure"},
    {"symbol": "IRCTC", "name": "IRCTC", "sector": "Infrastructure"},
    {"symbol": "HUDCO", "name": "HUDCO", "sector": "Finance"},
    {"symbol": "PFC", "name": "Power Finance Corporation", "sector": "Finance"},
    {"symbol": "RECLTD", "name": "REC Limited", "sector": "Finance"},
    # Metals & Mining
    {"symbol": "VEDL", "name": "Vedanta", "sector": "Metals"},
    {"symbol": "NMDC", "name": "NMDC", "sector": "Mining"},
    {"symbol": "SAIL", "name": "Steel Authority of India", "sector": "Metals"},
    {"symbol": "NATIONALUM", "name": "National Aluminium", "sector": "Metals"},
    {"symbol": "HINDCOPPER", "name": "Hindustan Copper", "sector": "Metals"},
    {"symbol": "MOIL", "name": "MOIL", "sector": "Mining"},
    # Finance / NBFC
    {"symbol": "BAJAJHLDNG", "name": "Bajaj Holdings", "sector": "Finance"},
    {"symbol": "CHOLAFIN", "name": "Cholamandalam Finance", "sector": "Finance"},
    {"symbol": "MUTHOOTFIN", "name": "Muthoot Finance", "sector": "Finance"},
    {"symbol": "MANAPPURAM", "name": "Manappuram Finance", "sector": "Finance"},
    {"symbol": "M&MFIN", "name": "M&M Financial Services", "sector": "Finance"},
    {"symbol": "SHRIRAMFIN", "name": "Shriram Finance", "sector": "Finance"},
    {"symbol": "LICHSGFIN", "name": "LIC Housing Finance", "sector": "Finance"},
    # Insurance
    {"symbol": "ICICIPRULI", "name": "ICICI Prudential Life", "sector": "Insurance"},
    {"symbol": "ICICIGI", "name": "ICICI Lombard General Insurance", "sector": "Insurance"},
    {"symbol": "NIACL", "name": "New India Assurance", "sector": "Insurance"},
    {"symbol": "STARHEALTH", "name": "Star Health Insurance", "sector": "Insurance"},
    # Cement
    {"symbol": "ACC", "name": "ACC", "sector": "Cement"},
    {"symbol": "AMBUJACEM", "name": "Ambuja Cements", "sector": "Cement"},
    {"symbol": "SHREECEM", "name": "Shree Cement", "sector": "Cement"},
    {"symbol": "RAMCOCEM", "name": "Ramco Cements", "sector": "Cement"},
    # Telecom
    {"symbol": "IDEA", "name": "Vodafone Idea", "sector": "Telecom"},
    {"symbol": "TATACOMM", "name": "Tata Communications", "sector": "Telecom"},
    # Real Estate
    {"symbol": "DLF", "name": "DLF", "sector": "Real Estate"},
    {"symbol": "GODREJPROP", "name": "Godrej Properties", "sector": "Real Estate"},
    {"symbol": "OBEROIRLTY", "name": "Oberoi Realty", "sector": "Real Estate"},
    {"symbol": "PRESTIGE", "name": "Prestige Estates", "sector": "Real Estate"},
    {"symbol": "BRIGADE", "name": "Brigade Enterprises", "sector": "Real Estate"},
    # Defense / Aerospace
    {"symbol": "COCHINSHIP", "name": "Cochin Shipyard", "sector": "Defense"},
    {"symbol": "MAZAGON", "name": "Mazagon Dock Shipbuilders", "sector": "Defense"},
    {"symbol": "GRSE", "name": "Garden Reach Shipbuilders", "sector": "Defense"},
    {"symbol": "BEML", "name": "BEML", "sector": "Defense"},
    # New-age / Internet
    {"symbol": "NYKAA", "name": "Nykaa (FSN E-Commerce)", "sector": "Consumer"},
    {"symbol": "PAYTM", "name": "Paytm (One 97 Communications)", "sector": "Fintech"},
    {"symbol": "POLICYBZR", "name": "PB Fintech (PolicyBazaar)", "sector": "Fintech"},
    {"symbol": "DELHIVERY", "name": "Delhivery", "sector": "Logistics"},
    # Indices (for reference)
    {"symbol": "^NSEI", "name": "NIFTY 50 Index", "sector": "Index"},
    {"symbol": "^BSESN", "name": "BSE SENSEX Index", "sector": "Index"},
]

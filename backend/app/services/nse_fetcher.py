"""
NSE India data fetcher using NseIndiaApi (pip install nse[local]).

Primary data source — replaces manual cookie scraping.
Provides: live quotes, historical OHLCV, market status, gainers/losers,
index data, and options chain.

Rate limit: 3 req/sec (managed by the nse library's built-in throttle).
Free. No API key required.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ── Lazy-loaded singleton NSE client ─────────────────────────
_nse_instance = None
_nse_init_time: float = 0
_NSE_SESSION_TTL = 300  # 5 min — NSE sessions expire

_DOWNLOAD_DIR = Path("/tmp/agentx_nse")


def _get_nse():
    """Get or create the NSE client singleton. Thread-safe via GIL."""
    global _nse_instance, _nse_init_time
    now = time.time()

    if _nse_instance and (now - _nse_init_time) < _NSE_SESSION_TTL:
        return _nse_instance

    # Close old session if exists
    if _nse_instance:
        try:
            _nse_instance.exit()
        except Exception:
            pass

    try:
        from nse import NSE
        _DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        _nse_instance = NSE(_DOWNLOAD_DIR)
        _nse_init_time = now
        logger.info("NSE client initialized")
        return _nse_instance
    except Exception as e:
        logger.warning("Failed to initialize NSE client: %s", e)
        _nse_instance = None
        return None


# ── Live quote ───────────────────────────────────────────────

def _sync_fetch_quote(symbol: str) -> Optional[dict]:
    """Fetch live quote via NseIndiaApi. Returns normalized dict."""
    try:
        nse = _get_nse()
        if not nse:
            return None

        data = nse.quote(symbol, type="equity")
        if not data:
            return None

        price_info = data.get("priceInfo", {})
        info = data.get("info", {})
        metadata = data.get("metadata", {})
        intraday = price_info.get("intraDayHighLow", {})
        week_hl = price_info.get("weekHighLow", {})
        sec_info = data.get("securityInfo", {})

        return {
            "symbol": symbol,
            "lastPrice": price_info.get("lastPrice"),
            "change": price_info.get("change"),
            "pChange": price_info.get("pChange"),
            "open": price_info.get("open"),
            "close": price_info.get("close"),
            "previousClose": price_info.get("previousClose"),
            "high": intraday.get("max"),
            "low": intraday.get("min"),
            "vwap": price_info.get("vwap"),
            "totalTradedVolume": sec_info.get("tradedVolume"),
            "totalTradedValue": sec_info.get("tradedValue"),
            "weekHigh": week_hl.get("max"),
            "weekLow": week_hl.get("min"),
            "upperCP": price_info.get("upperCP"),
            "lowerCP": price_info.get("lowerCP"),
            "name": info.get("companyName", symbol),
            "industry": info.get("industry"),
            "isFnO": info.get("isFNOSec", False),
            "pe": metadata.get("pdSymbolPe"),
            "sectorPe": metadata.get("pdSectorPe"),
            "source": "nse_api",
        }
    except Exception as e:
        logger.debug("NSE quote failed for %s: %s", symbol, e)
        return None


async def nse_fetch_quote(symbol: str) -> Optional[dict]:
    """Async wrapper for live NSE quote."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_fetch_quote, symbol)


# ── OHLCV (equityQuote — current day summary) ───────────────

def _sync_fetch_ohlcv_today(symbol: str) -> Optional[dict]:
    """Fetch today's OHLCV summary via equityQuote."""
    try:
        nse = _get_nse()
        if not nse:
            return None
        data = nse.equityQuote(symbol)
        if data and data.get("close"):
            return data
        return None
    except Exception as e:
        logger.debug("NSE equityQuote failed for %s: %s", symbol, e)
        return None


async def nse_fetch_ohlcv(symbol: str) -> Optional[pd.DataFrame]:
    """Fetch recent OHLCV as DataFrame. Uses historical data API."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_fetch_historical_df, symbol, 180)


# ── Historical OHLCV ────────────────────────────────────────

def _sync_fetch_historical_df(symbol: str, days: int = 365) -> Optional[pd.DataFrame]:
    """Fetch historical OHLCV bars from NSE and return as pandas DataFrame."""
    try:
        nse = _get_nse()
        if not nse:
            return None

        to_date = date.today()
        from_date = to_date - timedelta(days=days)

        rows = nse.fetch_equity_historical_data(symbol, from_date, to_date)
        if not rows:
            logger.debug("NSE historical returned no data for %s", symbol)
            return None

        records = []
        for r in rows:
            try:
                records.append({
                    "Date": pd.Timestamp(datetime.strptime(r["mtimestamp"], "%d-%b-%Y")),
                    "Open": float(r.get("chOpeningPrice", 0)),
                    "High": float(r.get("chTradeHighPrice", 0)),
                    "Low": float(r.get("chTradeLowPrice", 0)),
                    "Close": float(r.get("chClosingPrice", 0)),
                    "Volume": float(r.get("chTotTradedQty", 0)),
                })
            except (ValueError, KeyError):
                continue

        if not records:
            return None

        df = pd.DataFrame(records)
        df.set_index("Date", inplace=True)
        df.sort_index(inplace=True)
        return df
    except Exception as e:
        logger.debug("NSE historical fetch failed for %s: %s", symbol, e)
        return None


async def nse_fetch_history(
    symbol: str, days: int = 365,
) -> Optional[pd.DataFrame]:
    """Async wrapper for historical OHLCV."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_fetch_historical_df, symbol, days)


# ── Market status ────────────────────────────────────────────

def _sync_market_status() -> Optional[list[dict]]:
    """Fetch real NSE market status."""
    try:
        nse = _get_nse()
        if not nse:
            return None
        return nse.status()
    except Exception as e:
        logger.debug("NSE status failed: %s", e)
        return None


async def nse_market_status() -> Optional[list[dict]]:
    """Async wrapper for market status."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_market_status)


# ── Index data ───────────────────────────────────────────────

def _sync_fetch_index_quote() -> Optional[dict]:
    """Fetch NIFTY 50 quote from NSE status endpoint."""
    try:
        statuses = _sync_market_status()
        if not statuses:
            return None
        result = {}
        for s in statuses:
            idx_name = s.get("index", "")
            if idx_name in ("NIFTY 50", "NIFTY BANK", "INDIA VIX"):
                result[idx_name] = {
                    "last": s.get("last"),
                    "variation": s.get("variation"),
                    "percentChange": s.get("percentChange"),
                    "marketStatus": s.get("marketStatus"),
                    "tradeDate": s.get("tradeDate"),
                }
        return result
    except Exception as e:
        logger.debug("NSE index fetch failed: %s", e)
        return None


async def nse_fetch_indices() -> Optional[dict]:
    """Async wrapper for index data."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_fetch_index_quote)


# ── Options chain ────────────────────────────────────────────

def _sync_fetch_option_chain(symbol: str) -> Optional[dict]:
    """Fetch options chain data for FnO stocks."""
    try:
        nse = _get_nse()
        if not nse:
            return None
        data = nse.optionChain(symbol)
        if not data:
            return None
        return data
    except Exception as e:
        logger.debug("NSE option chain failed for %s: %s", symbol, e)
        return None


async def nse_fetch_option_chain(symbol: str) -> Optional[dict]:
    """Async wrapper for options chain."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_fetch_option_chain, symbol)


# ── Cleanup ──────────────────────────────────────────────────

def shutdown_nse():
    """Call on app shutdown to close the NSE session cleanly."""
    global _nse_instance
    if _nse_instance:
        try:
            _nse_instance.exit()
        except Exception:
            pass
        _nse_instance = None

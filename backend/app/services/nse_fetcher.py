"""
NSE India fallback data fetcher.
Free public JSON APIs — no authentication required.
Used when yfinance is unavailable or repeatedly failing.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

_session: Optional[requests.Session] = None
_session_time: float = 0
_SESSION_TTL = 300  # 5 min — NSE cookies expire quickly


def _get_session() -> requests.Session:
    """Get or create a requests session with NSE cookies."""
    global _session, _session_time
    now = time.time()
    if _session and (now - _session_time) < _SESSION_TTL:
        return _session
    s = requests.Session()
    s.headers.update(HEADERS)
    # Hit homepage to obtain cookies required by NSE API
    s.get("https://www.nseindia.com", timeout=10)
    _session = s
    _session_time = time.time()
    return s


def _sync_fetch_quote(symbol: str) -> Optional[dict]:
    """Synchronous NSE quote fetch (called via run_in_executor)."""
    try:
        session = _get_session()
        url = f"https://www.nseindia.com/api/quote-equity?symbol={symbol}"
        resp = session.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        price_info = data.get("priceInfo", {})
        return {
            "symbol": symbol,
            "lastPrice": price_info.get("lastPrice"),
            "change": price_info.get("change"),
            "pChange": price_info.get("pChange"),
            "open": price_info.get("open"),
            "close": price_info.get("close"),
            "previousClose": price_info.get("previousClose"),
            "high": data.get("priceInfo", {}).get("intraDayHighLow", {}).get("max"),
            "low": data.get("priceInfo", {}).get("intraDayHighLow", {}).get("min"),
            "totalTradedVolume": data.get("securityWiseDP", {}).get("quantityTraded"),
            "source": "nse",
        }
    except Exception as e:
        logger.warning("NSE quote fetch failed for %s: %s", symbol, e)
        return None


def _sync_fetch_ohlcv(symbol: str) -> Optional[pd.DataFrame]:
    """Synchronous NSE intraday chart data fetch (called via run_in_executor)."""
    try:
        session = _get_session()
        url = (
            f"https://www.nseindia.com/api/chart-databyindex"
            f"?index={symbol}&indices=false"
        )
        resp = session.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        graph_data = data.get("gpiData", data.get("graphData", []))
        if not graph_data:
            logger.warning("NSE chart returned empty data for %s", symbol)
            return None

        rows = []
        for point in graph_data:
            # NSE chart API returns [timestamp_ms, price] pairs
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                ts = pd.Timestamp(point[0], unit="ms", tz="Asia/Kolkata")
                rows.append({"Date": ts, "Close": point[1]})

        if not rows:
            return None

        df = pd.DataFrame(rows)
        df.set_index("Date", inplace=True)
        return df
    except Exception as e:
        logger.warning("NSE OHLCV fetch failed for %s: %s", symbol, e)
        return None


async def nse_fetch_quote(symbol: str) -> Optional[dict]:
    """Fetch live quote from NSE. Returns dict with price, change, volume, etc."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_fetch_quote, symbol)


async def nse_fetch_ohlcv(symbol: str) -> Optional[pd.DataFrame]:
    """Fetch intraday OHLCV from NSE chart API. Returns DataFrame or None."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_fetch_ohlcv, symbol)

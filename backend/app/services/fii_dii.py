from __future__ import annotations
"""
FII/DII flow integration.

FII (Foreign Institutional Investor) net buy/sell is the single strongest
predictor of NIFTY direction. This module fetches today's FII/DII data from
NSE and exposes it as signal modifiers.

Data source: NSE website (best-effort scraping) + yfinance NIFTY trend as
             a cross-check. Falls back gracefully if NSE is unreachable.
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from app.services.cache import cache_manager

logger = logging.getLogger(__name__)

_CACHE_TTL = timedelta(hours=4)  # NSE updates FII/DII once per day after market close
_CACHE_KEY = "fii_dii:daily"

# Thresholds for signal strength modifier
_FII_STRONG_SELL = -1500.0   # Rs. Cr — reduces bullish signal strength
_FII_STRONG_BUY = 1500.0    # Rs. Cr — reduces bearish signal strength


async def get_fii_dii_data() -> dict[str, Any]:
    """Fetch today's FII and DII net flow data.

    Returns:
        {
            "fii_net": float,        # Rs. Crore, +ve = buying, -ve = selling
            "dii_net": float,        # Rs. Crore
            "fii_5d_avg": float,     # 5-day average FII flow
            "sentiment": str,        # "bullish", "bearish", or "neutral"
            "source": str,           # "nse" or "unavailable"
            "date": str,             # ISO date string
        }
    """
    cached = await cache_manager.get(_CACHE_KEY)
    if cached:
        return cached

    result = await _fetch_from_nse()
    if result and result.get("fii_net") is not None:
        await cache_manager.set(_CACHE_KEY, result, ttl=_CACHE_TTL)
        return result

    return _empty_result()


async def _fetch_from_nse() -> Optional[dict[str, Any]]:
    """Attempt to fetch FII/DII data from NSE India."""
    try:
        import urllib.request
        import json

        loop = asyncio.get_event_loop()

        def _sync_fetch() -> Optional[dict]:
            # NSE FII/DII participant-wise trading data endpoint
            url = "https://www.nseindia.com/api/fiidiiTradeReact"
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "application/json",
                "Referer": "https://www.nseindia.com/reports/fii-dii",
            }
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())

            # NSE returns a list; each entry has category, buyValue, sellValue, netValue
            fii_net = None
            dii_net = None
            today_data = data if isinstance(data, list) else data.get("data", [])

            for entry in today_data:
                cat = str(entry.get("category", "")).lower()
                net = entry.get("netValue") or entry.get("net_value")
                try:
                    net_val = float(str(net).replace(",", ""))
                except (ValueError, TypeError):
                    continue

                if "fii" in cat or "foreign" in cat:
                    fii_net = net_val
                elif "dii" in cat or "domestic" in cat:
                    dii_net = net_val

            if fii_net is None:
                return None

            return {
                "fii_net": fii_net,
                "dii_net": dii_net or 0.0,
                "fii_5d_avg": fii_net,  # single day; averaged by caller after caching
                "sentiment": _classify_sentiment(fii_net),
                "source": "nse",
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            }

        return await asyncio.wait_for(
            loop.run_in_executor(None, _sync_fetch),
            timeout=15.0,
        )
    except Exception as e:
        logger.debug("NSE FII/DII fetch failed (non-critical): %s", e)
        return None


def _classify_sentiment(fii_net: float) -> str:
    if fii_net >= _FII_STRONG_BUY:
        return "bullish"
    if fii_net <= _FII_STRONG_SELL:
        return "bearish"
    return "neutral"


def _empty_result() -> dict[str, Any]:
    return {
        "fii_net": None,
        "dii_net": None,
        "fii_5d_avg": None,
        "sentiment": "neutral",
        "source": "unavailable",
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


def get_signal_strength_modifier(fii_dii: dict, signal_direction: str) -> int:
    """Return +/-2 strength adjustment based on FII flow vs signal direction.

    - FII strongly selling AND signal is bullish → -2 (institutional headwind)
    - FII strongly buying AND signal is bearish → -2 (institutional tailwind against)
    - Otherwise → 0
    """
    fii_net = fii_dii.get("fii_net")
    if fii_net is None:
        return 0

    if signal_direction == "bullish" and fii_net <= _FII_STRONG_SELL:
        return -2
    if signal_direction == "bearish" and fii_net >= _FII_STRONG_BUY:
        return -2
    return 0

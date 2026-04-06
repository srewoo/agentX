from __future__ import annotations
"""
Corporate governance data — promoter pledge % and insider trading alerts.

Heavily pledged promoter shares are a red flag. When price falls, pledged
shares get margin-called → forced selling → further price drop (death spiral).
Pledging > 50% of promoter holdings is considered high risk on NSE/BSE.

Data source: BSE shareholding pattern filings (quarterly), fetched via NSE API.
Falls back gracefully if unavailable.
"""
import asyncio
import logging
from datetime import timedelta
from typing import Any, Optional

from app.services.cache import cache_manager

logger = logging.getLogger(__name__)

_CACHE_TTL = timedelta(hours=24)  # shareholding data is quarterly


async def get_promoter_pledge_data(symbol: str) -> dict[str, Any]:
    """Fetch promoter pledging % for a stock.

    Returns:
        {
            "symbol": str,
            "promoter_holding_pct": float or None,
            "pledged_pct": float or None,       # % of promoter shares pledged
            "pledged_of_total_pct": float or None,  # % of total equity pledged
            "risk_level": str,                  # "low", "medium", "high", "critical"
            "source": str,
        }
    """
    cache_key = f"corporate_governance:pledge:{symbol}"
    cached = await cache_manager.get(cache_key)
    if cached:
        return cached

    result = await _fetch_pledge_data(symbol)
    if result and result.get("pledged_pct") is not None:
        await cache_manager.set(cache_key, result, ttl=_CACHE_TTL)
        return result

    return _empty_pledge_result(symbol)


async def _fetch_pledge_data(symbol: str) -> Optional[dict[str, Any]]:
    """Try to fetch promoter pledging data from NSE/BSE sources."""
    try:
        import urllib.request
        import json

        loop = asyncio.get_event_loop()

        def _sync_fetch() -> Optional[dict]:
            # NSE shareholding pattern API
            url = f"https://www.nseindia.com/api/corporate-share-holdings-master?symbol={symbol}&params=latestFillings"
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "Accept": "application/json",
                "Referer": "https://www.nseindia.com",
            }
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())

            # Parse promoter category data
            promoter_holding = None
            pledged_pct = None
            pledged_of_total = None

            entries = data if isinstance(data, list) else data.get("data", [])
            for entry in entries:
                category = str(entry.get("shareholderCategory", "")).lower()
                if "promoter" in category:
                    try:
                        promoter_holding = float(entry.get("percentageOfShares", 0))
                        pledged_shares = float(entry.get("pledgedOrEncumbered", 0) or 0)
                        total_shares = float(entry.get("noOfShares", 1) or 1)
                        if promoter_holding > 0 and total_shares > 0:
                            pledged_pct = round(pledged_shares / total_shares * 100, 2)
                            pledged_of_total = round(pledged_pct * promoter_holding / 100, 2)
                    except (ValueError, TypeError):
                        pass
                    break

            if promoter_holding is None:
                return None

            return {
                "symbol": symbol,
                "promoter_holding_pct": promoter_holding,
                "pledged_pct": pledged_pct,
                "pledged_of_total_pct": pledged_of_total,
                "risk_level": _classify_pledge_risk(pledged_pct),
                "source": "nse",
            }

        return await asyncio.wait_for(
            loop.run_in_executor(None, _sync_fetch),
            timeout=12.0,
        )
    except Exception as e:
        logger.debug("Promoter pledge fetch failed for %s (non-critical): %s", symbol, e)
        return None


def _classify_pledge_risk(pledged_pct: Optional[float]) -> str:
    if pledged_pct is None:
        return "unknown"
    if pledged_pct >= 75:
        return "critical"
    if pledged_pct >= 50:
        return "high"
    if pledged_pct >= 25:
        return "medium"
    return "low"


def _empty_pledge_result(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "promoter_holding_pct": None,
        "pledged_pct": None,
        "pledged_of_total_pct": None,
        "risk_level": "unknown",
        "source": "unavailable",
    }


def get_pledge_strength_modifier(pledge_data: dict) -> int:
    """Return signal strength modifier based on promoter pledge level.

    High pledge → reduce bullish signal strength (forced selling risk).
    Critical pledge → reduce by 3 (serious red flag).
    """
    risk = pledge_data.get("risk_level", "unknown")
    if risk == "critical":
        return -3
    if risk == "high":
        return -2
    if risk == "medium":
        return -1
    return 0

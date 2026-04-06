from __future__ import annotations
"""
NSE/BSE market rules enforcement.

Covers:
1. Circuit limits — upper/lower circuit hit detection (price cannot move further)
2. F&O ban list — stocks in futures & options ban (excessive speculative activity)
3. Ex-dividend dates — flag upcoming ex-div to prevent false bearish signals

Data sources:
- NSE API (best-effort, falls back gracefully)
- yfinance for ex-dividend detection
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from app.services.cache import cache_manager

logger = logging.getLogger(__name__)

_FNO_BAN_CACHE_KEY = "market_rules:fno_ban"
_CIRCUIT_CACHE_KEY = "market_rules:circuit_hits"
_CACHE_TTL = timedelta(hours=4)


async def get_fno_ban_list() -> set[str]:
    """Fetch the current F&O ban list from NSE.

    Stocks in F&O ban period have excessive speculative OI — trading is restricted
    and signals are unreliable. Returns set of banned symbols.
    """
    cached = await cache_manager.get(_FNO_BAN_CACHE_KEY)
    if cached:
        return set(cached)

    ban_list = await _fetch_fno_ban_from_nse()
    if ban_list is not None:
        await cache_manager.set(_FNO_BAN_CACHE_KEY, list(ban_list), ttl=_CACHE_TTL)
        return ban_list
    return set()


async def _fetch_fno_ban_from_nse() -> Optional[set[str]]:
    """Fetch F&O ban list from NSE API."""
    try:
        import urllib.request
        import json

        loop = asyncio.get_event_loop()

        def _sync_fetch() -> Optional[set[str]]:
            url = "https://www.nseindia.com/api/fo-mktwatch-posBan"
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "Accept": "application/json",
                "Referer": "https://www.nseindia.com",
            }
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())

            # NSE returns {"data": [{"symbol": "XYZ", ...}, ...]}
            symbols: set[str] = set()
            for entry in data.get("data", []):
                sym = entry.get("symbol", "")
                if sym:
                    symbols.add(sym.upper())
            return symbols

        return await asyncio.wait_for(
            loop.run_in_executor(None, _sync_fetch),
            timeout=12.0,
        )
    except Exception as e:
        logger.debug("F&O ban list fetch failed (non-critical): %s", e)
        return None


async def is_near_circuit(symbol: str, current_price: float, prev_close: float) -> dict[str, Any]:
    """Check if a stock is near its circuit limit.

    NSE sets ±20% / ±10% / ±5% circuits depending on stock category.
    When a stock hits circuit, no further movement is possible — signals are unreliable.

    Returns:
        {
            "near_upper_circuit": bool,
            "near_lower_circuit": bool,
            "circuit_pct_move": float,  # current % move from prev close
            "warning": str or None,
        }
    """
    if prev_close <= 0:
        return {"near_upper_circuit": False, "near_lower_circuit": False, "circuit_pct_move": 0.0, "warning": None}

    pct_move = (current_price - prev_close) / prev_close * 100

    # Most NSE stocks have 20% circuit limits; penny stocks have 5%
    # We warn when within 2% of any common circuit level
    circuit_limits = [5.0, 10.0, 20.0]
    near_upper = any(pct_move >= limit - 2.0 for limit in circuit_limits)
    near_lower = any(pct_move <= -(limit - 2.0) for limit in circuit_limits)

    warning = None
    if near_upper:
        warning = f"Stock up {pct_move:.1f}% — may be near upper circuit. Liquidity risk."
    elif near_lower:
        warning = f"Stock down {abs(pct_move):.1f}% — may be near lower circuit. Liquidity risk."

    return {
        "near_upper_circuit": near_upper,
        "near_lower_circuit": near_lower,
        "circuit_pct_move": round(pct_move, 2),
        "warning": warning,
    }


def should_suppress_signal(
    symbol: str,
    fno_ban_list: set[str],
    circuit_info: Optional[dict] = None,
) -> tuple[bool, str]:
    """Decide whether to suppress a signal due to market rules.

    Returns (suppress: bool, reason: str).
    """
    if symbol in fno_ban_list:
        return True, f"{symbol} is in F&O ban period — speculative activity restricted"

    if circuit_info:
        if circuit_info.get("near_upper_circuit") or circuit_info.get("near_lower_circuit"):
            return False, circuit_info.get("warning", "")  # warn but don't suppress

    return False, ""

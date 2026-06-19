from __future__ import annotations
"""Free, keyless FX source for USD/INR.

Finnhub's forex endpoints are paid-tier only (a free key 403s with "You don't
have access to this resource"), and yfinance ``INR=X`` is unreliable, so we use
free no-key FX rate APIs as the dependable source: open.er-api.com first,
frankfurter.app (ECB reference rates) as fallback.

Best-effort and never raises: returns ``None`` on any failure so callers fall
back to their next source instead of going blind. Honours ``source_health``.
"""
import asyncio
import logging
from typing import Optional

from app.services import source_health

logger = logging.getLogger(__name__)

SOURCE = "fx_free"
_TIMEOUT = 10.0


def _parse_inr(data: object) -> Optional[float]:
    """Extract a positive INR rate from a ``{"rates": {"INR": ...}}`` payload."""
    if not isinstance(data, dict):
        return None
    inr = (data.get("rates") or {}).get("INR")
    try:
        rate = float(inr)
    except (TypeError, ValueError):
        return None
    return round(rate, 4) if rate > 0 else None


def _fetch_sync() -> Optional[float]:
    import requests

    sources = [
        ("https://open.er-api.com/v6/latest/USD", None),
        ("https://api.frankfurter.app/latest", {"from": "USD", "to": "INR"}),
    ]
    for url, params in sources:
        try:
            resp = requests.get(url, params=params, timeout=_TIMEOUT)
            resp.raise_for_status()
            rate = _parse_inr(resp.json())
            if rate is not None:
                return rate
        except Exception as e:
            logger.debug("fx_fetcher %s failed: %s", url, e)
    return None


async def get_usd_inr() -> Optional[float]:
    """Current USD/INR from a free keyless FX API. ``None`` on cooldown/failure."""
    if source_health.is_down(SOURCE):
        return None
    loop = asyncio.get_event_loop()
    rate = await loop.run_in_executor(None, _fetch_sync)
    if rate is None:
        source_health.mark_down(SOURCE)
        return None
    source_health.mark_up(SOURCE)
    return rate

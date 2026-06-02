from __future__ import annotations
"""Financial Modeling Prep (FMP) integration.

FMP is the best fundamentals/earnings API with usable NSE/BSE coverage. We
use it here for one high-value, immediately-wired job: the **earnings
calendar** that powers the risk gate's earnings-blackout rule. Indian results
days are the single most reliable way to get gapped against, so refusing to
open a fresh position within ±N days of a result is pure downside protection.

Keyed via the ``fmp_api_key`` setting. Honours the shared ``source_health``
cooldown so a flaky key / rate-limit doesn't get hammered per symbol. Every
function is best-effort: it returns ``None`` (never raises) so a missing key
or a failed fetch leaves the earnings-blackout gate *inert* rather than
blocking trades on missing data.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.services import source_health

logger = logging.getLogger(__name__)

SOURCE = "fmp"
_BASE = "https://financialmodelingprep.com/api/v3"
_TIMEOUT = 12.0

# The earnings calendar for a given window is stable across a trading day, and
# the gate queries it once per candidate — cache per (from,to) for an hour.
_CALENDAR_TTL = 3600.0
_calendar_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}


def _norm(symbol: str) -> str:
    """Bare symbol, exchange-suffix and case independent (RELIANCE.NS → RELIANCE)."""
    s = (symbol or "").upper().strip()
    for suffix in (".NS", ".BO", ".NSE", ".BSE"):
        if s.endswith(suffix):
            return s[: -len(suffix)]
    return s


def is_blackout(rows: list[dict[str, Any]], symbol: str) -> bool:
    """True when ``symbol`` appears in the earnings-calendar ``rows``.

    Pure function — the rows are assumed to already be scoped to the date
    window of interest (FMP's ``earning_calendar`` filters by from/to).
    """
    target = _norm(symbol)
    if not target:
        return False
    return any(_norm(str(r.get("symbol", ""))) == target for r in rows)


async def _get_api_key() -> Optional[str]:
    try:
        from app.services.orchestrator import _get_settings
        settings = await _get_settings()
        key = settings.get("fmp_api_key")
        return key or None
    except Exception as e:
        logger.debug("fmp: settings load failed: %s", e)
        return None


def _fetch_earnings_sync(from_date: str, to_date: str, api_key: str) -> Optional[list[dict[str, Any]]]:
    import requests
    try:
        resp = requests.get(
            f"{_BASE}/earning_calendar",
            params={"from": from_date, "to": to_date, "apikey": api_key},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        # FMP returns {"Error Message": ...} on a bad/over-limit key.
        logger.debug("fmp earnings unexpected payload: %s", str(data)[:200])
        return None
    except Exception as e:
        logger.debug("fmp earnings fetch failed: %s", e)
        return None


async def get_earnings_calendar(
    from_date: str, to_date: str
) -> Optional[list[dict[str, Any]]]:
    """Earnings calendar rows for ``[from_date, to_date]`` (ISO YYYY-MM-DD).

    Returns ``None`` when no key is configured or the source is in cooldown /
    failed. Cached per window for an hour.
    """
    cache_key = (from_date, to_date)
    cached = _calendar_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _CALENDAR_TTL:
        return cached[1]

    if source_health.is_down(SOURCE):
        return None
    api_key = await _get_api_key()
    if not api_key:
        return None

    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(
        None, _fetch_earnings_sync, from_date, to_date, api_key
    )
    if rows is None:
        source_health.mark_down(SOURCE)
        return None
    source_health.mark_up(SOURCE)
    _calendar_cache[cache_key] = (time.time(), rows)
    return rows


async def is_in_earnings_blackout(
    symbol: str, *, within_days: int = 3, now: Optional[datetime] = None
) -> Optional[bool]:
    """Whether ``symbol`` has results within ±``within_days``.

    Returns ``None`` when the calendar is unavailable (no key / fetch failed)
    so the caller can leave the gate inert; ``True``/``False`` otherwise.
    """
    now = now or datetime.now(timezone.utc)
    frm = (now - timedelta(days=within_days)).date().isoformat()
    to = (now + timedelta(days=within_days)).date().isoformat()
    rows = await get_earnings_calendar(frm, to)
    if rows is None:
        return None
    return is_blackout(rows, symbol)


def _reset_cache() -> None:
    """Test helper — clear the calendar cache."""
    _calendar_cache.clear()

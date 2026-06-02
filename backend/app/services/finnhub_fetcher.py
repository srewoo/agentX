from __future__ import annotations
"""Finnhub integration — macro / forex.

Finnhub's strength for an India-focused system isn't Indian equities (those
are paid/enterprise) — it's clean **global macro and forex**. We use it for
the one number the live-macro injection (9pt.md §2) leans on most and that
yfinance serves least reliably: **USD/INR**. A real-time, well-maintained
USD/INR feed is what lets the LLM layer reason about "today's" rupee rather
than the playbook in the abstract.

Keyed via ``finnhub_api_key``. Honours ``source_health`` cooldown and is
best-effort: returns ``None`` on any failure so callers fall back to their
existing source (yfinance ``INR=X``) instead of going blind.
"""
import asyncio
import logging
from typing import Any, Optional

from app.services import source_health

logger = logging.getLogger(__name__)

SOURCE = "finnhub"
_BASE = "https://finnhub.io/api/v1"
_TIMEOUT = 10.0


def parse_forex_rate(data: Any, quote_ccy: str = "INR") -> Optional[float]:
    """Extract a quote-currency rate from Finnhub's ``/forex/rates`` payload.

    Shape: ``{"base": "USD", "quote": {"INR": 83.2, "EUR": 0.92, ...}}``.
    Pure function so the parse is unit-tested without a live key.
    """
    if not isinstance(data, dict):
        return None
    quote = data.get("quote")
    if not isinstance(quote, dict):
        return None
    val = quote.get(quote_ccy) or quote.get(quote_ccy.upper())
    try:
        rate = float(val)
    except (TypeError, ValueError):
        return None
    return rate if rate > 0 else None


async def _get_api_key() -> Optional[str]:
    try:
        from app.services.orchestrator import _get_settings
        settings = await _get_settings()
        return settings.get("finnhub_api_key") or None
    except Exception as e:
        logger.debug("finnhub: settings load failed: %s", e)
        return None


def _fetch_forex_sync(api_key: str, base: str = "USD") -> Optional[dict[str, Any]]:
    import requests
    try:
        resp = requests.get(
            f"{_BASE}/forex/rates",
            params={"base": base, "token": api_key},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.debug("finnhub forex fetch failed: %s", e)
        return None


async def get_usd_inr() -> Optional[float]:
    """Current USD/INR from Finnhub. ``None`` when no key / cooldown / failure."""
    if source_health.is_down(SOURCE):
        return None
    api_key = await _get_api_key()
    if not api_key:
        return None
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _fetch_forex_sync, api_key, "USD")
    if data is None:
        source_health.mark_down(SOURCE)
        return None
    rate = parse_forex_rate(data, "INR")
    if rate is None:
        source_health.mark_down(SOURCE)
        return None
    source_health.mark_up(SOURCE)
    return round(rate, 4)

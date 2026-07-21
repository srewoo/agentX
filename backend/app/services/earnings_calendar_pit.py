from __future__ import annotations
"""3.4 — Historical (point-in-time) earnings calendar for backtests.

The earnings blackout — skip a name when results land inside the next few
sessions — is applied LIVE (``fmp_fetcher.is_in_earnings_blackout`` +
``get_upcoming_results_dates``) but NOT in backtests, because the live source is
a forward-looking API with no history. So the backtest trades through earnings
the live engine would have sat out: a live/backtest behavioural mismatch that
flatters the backtest.

This module supplies a point-in-time historical earnings calendar so the SAME
blackout rule can run on old bars. It reads an optional data drop
(``models/earnings_calendar.csv`` — columns ``symbol,earnings_date``); absent,
every check returns False so the blackout is simply inert (matching the live
fail-open, never a crash).

The blackout test is pure so it unit-tests without any data or IO.
"""
import csv
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CALENDAR_CSV = Path(__file__).resolve().parent.parent.parent / "models" / "earnings_calendar.csv"
DEFAULT_BLACKOUT_WINDOW_DAYS = 5   # matches orchestrator's live window_days=5

_cache: Optional[dict[str, list[date]]] = None


def _to_date(v: object) -> Optional[date]:
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


def is_in_blackout(
    earnings_dates: list[date], asof: date,
    window_days: int = DEFAULT_BLACKOUT_WINDOW_DAYS,
) -> bool:
    """Pure: True if any earnings date falls in the UPCOMING window
    ``(asof, asof + window_days]`` — i.e. results are imminent, so the live
    engine would sit the trade out. Past earnings do not blackout (that window
    is the PEAD opportunity the live engine keeps, not skips)."""
    if not earnings_dates:
        return False
    horizon = asof + timedelta(days=window_days)
    return any(asof < d <= horizon for d in earnings_dates)


def _load_calendar() -> dict[str, list[date]]:
    global _cache
    if _cache is not None:
        return _cache
    out: dict[str, list[date]] = {}
    if _CALENDAR_CSV.exists():
        try:
            with _CALENDAR_CSV.open(newline="") as f:
                for r in csv.DictReader(f):
                    sym = (r.get("symbol") or "").strip().upper()
                    d = _to_date(r.get("earnings_date"))
                    if sym and d:
                        out.setdefault(sym, []).append(d)
            for sym in out:
                out[sym].sort()
        except Exception as e:
            logger.warning("earnings_calendar_pit: failed to read %s: %s", _CALENDAR_CSV, e)
    _cache = out
    return out


def has_calendar() -> bool:
    """True when a historical earnings calendar is present to drive the check."""
    return bool(_load_calendar())


def is_in_blackout_at(
    symbol: str, asof: date, window_days: int = DEFAULT_BLACKOUT_WINDOW_DAYS,
) -> bool:
    """Historical blackout check for ``symbol`` as of ``asof``. False (inert)
    when no calendar is present — the honest fail-open the live path uses."""
    cal = _load_calendar()
    dates = cal.get((symbol or "").strip().upper())
    return is_in_blackout(dates or [], asof, window_days)


def _reset_cache() -> None:
    """Test hook."""
    global _cache
    _cache = None

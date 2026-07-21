from __future__ import annotations
"""Scan watchdog (Target-9 §5.5, lightweight) — catch a silently-stalled or
starved scan loop before it costs days of forward-test evidence.

Two failure modes, both learned the hard way (0 signals for 11 days went
unnoticed):

  * STALLED  — no scan has *completed* within ~one cadence period. The loop is
               dead/wedged. This is the classic "a dead loop must page within
               one cadence period" from ADR-005 / Target-9 §5.5.
  * STARVED  — scans keep completing but produce ZERO signals for hours while
               the market is open. The loop is alive but the funnel is broken
               (e.g. a degenerate meta-judge dropping 100% of candidates).

Both checks are gated on market-open so overnight/weekend quiet never pages.
The core is a pure function so it is trivially unit-tested; the caller passes
in the two timestamps the orchestrator already records.
"""
from datetime import datetime
from typing import Any, Optional

# A single missed run shouldn't page — require ~2.5 cadence periods of silence
# before declaring the loop stalled (two consecutive misses + slack).
_STALL_GRACE_MULT = 2.5
# Scans running but emitting zero signals for this many market-open hours is
# the "funnel starved" signature. ~one trading session.
_STARVE_HOURS = 6.0


def evaluate(
    *,
    now: datetime,
    last_scan_time: Optional[datetime],
    last_nonzero_scan_time: Optional[datetime],
    cadence_minutes: float,
    market_open: bool,
    stall_grace_mult: float = _STALL_GRACE_MULT,
    starve_hours: float = _STARVE_HOURS,
) -> dict[str, Any]:
    """Return the watchdog verdict.

    status ∈ {"ok", "stalled", "starved", "idle", "market_closed"}.
    severity ∈ {"ok", "warning", "critical"} for callers that page/alert.
    """
    mins_since_scan = (
        (now - last_scan_time).total_seconds() / 60.0 if last_scan_time else None
    )

    # When the market is closed the loop may legitimately pause and produce
    # nothing — never page for that. Report an informational status only.
    if not market_open:
        return {
            "status": "market_closed",
            "severity": "ok",
            "minutes_since_scan": round(mins_since_scan, 1) if mins_since_scan is not None else None,
        }

    if last_scan_time is None:
        return {
            "status": "idle",
            "severity": "warning",
            "minutes_since_scan": None,
            "reason": "no scan has completed yet since startup",
        }

    stall_limit = cadence_minutes * stall_grace_mult
    if mins_since_scan is not None and mins_since_scan > stall_limit:
        return {
            "status": "stalled",
            "severity": "critical",
            "minutes_since_scan": round(mins_since_scan, 1),
            "reason": (
                f"no scan completed in {mins_since_scan:.0f} min "
                f"(cadence {cadence_minutes:.0f}m, limit {stall_limit:.0f}m) — "
                f"scan loop appears dead"
            ),
        }

    hours_since_signal = (
        (now - last_nonzero_scan_time).total_seconds() / 3600.0
        if last_nonzero_scan_time
        else (now - last_scan_time).total_seconds() / 3600.0
    )
    if hours_since_signal > starve_hours:
        return {
            "status": "starved",
            "severity": "warning",
            "minutes_since_scan": round(mins_since_scan, 1) if mins_since_scan is not None else None,
            "hours_since_signal": round(hours_since_signal, 1),
            "reason": (
                f"scans running but 0 signals for {hours_since_signal:.1f}h of "
                f"market-open time — funnel may be broken (check filters/meta-judge)"
            ),
        }

    return {
        "status": "ok",
        "severity": "ok",
        "minutes_since_scan": round(mins_since_scan, 1) if mins_since_scan is not None else None,
        "hours_since_signal": round(hours_since_signal, 1),
    }


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


async def status() -> dict[str, Any]:
    """Live watchdog status from current orchestrator + settings state."""
    from datetime import timezone
    from app.services import orchestrator as orch
    from app.config import settings as app_settings

    now = datetime.now(timezone.utc)
    try:
        cadence = float(app_settings.default_alert_interval_minutes) or 15.0
    except Exception:
        cadence = 15.0
    if cadence <= 0:
        cadence = 15.0

    last_scan = _parse(getattr(orch, "last_scan_time", None))
    last_nonzero = _parse(getattr(orch, "last_nonzero_scan_time", None))
    try:
        market_open = orch.is_market_open()
    except Exception:
        market_open = False

    return evaluate(
        now=now,
        last_scan_time=last_scan,
        last_nonzero_scan_time=last_nonzero,
        cadence_minutes=cadence,
        market_open=market_open,
    )

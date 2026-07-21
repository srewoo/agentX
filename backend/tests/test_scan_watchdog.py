from __future__ import annotations
"""Tests for the scan watchdog (Target-9 §5.5).

Pins the two failure modes the '0 signals for 11 days' incident taught us:
a dead loop (STALLED) and a live-but-empty funnel (STARVED), plus the
market-closed suppression that stops overnight quiet from paging.
"""
from datetime import datetime, timezone, timedelta

from app.services.scan_watchdog import evaluate


def _t(minutes_ago=0, hours_ago=0):
    return datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc) - timedelta(
        minutes=minutes_ago, hours=hours_ago
    )


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


def test_healthy_scan_is_ok():
    v = evaluate(
        now=NOW,
        last_scan_time=_t(minutes_ago=5),
        last_nonzero_scan_time=_t(minutes_ago=20),
        cadence_minutes=15,
        market_open=True,
    )
    assert v["status"] == "ok"
    assert v["severity"] == "ok"


def test_stalled_loop_is_critical():
    # No scan in ~50 min with a 15-min cadence (limit = 15*2.5 = 37.5).
    v = evaluate(
        now=NOW,
        last_scan_time=_t(minutes_ago=50),
        last_nonzero_scan_time=_t(minutes_ago=50),
        cadence_minutes=15,
        market_open=True,
    )
    assert v["status"] == "stalled"
    assert v["severity"] == "critical"


def test_starved_funnel_is_warning():
    # Scans are recent (loop alive) but no signal for 8h of open market.
    v = evaluate(
        now=NOW,
        last_scan_time=_t(minutes_ago=3),
        last_nonzero_scan_time=_t(hours_ago=8),
        cadence_minutes=15,
        market_open=True,
    )
    assert v["status"] == "starved"
    assert v["severity"] == "warning"
    assert v["hours_since_signal"] >= 6.0


def test_market_closed_never_pages():
    # Even a long silence overnight must not flag while the market is closed.
    v = evaluate(
        now=NOW,
        last_scan_time=_t(hours_ago=14),
        last_nonzero_scan_time=_t(hours_ago=14),
        cadence_minutes=15,
        market_open=False,
    )
    assert v["status"] == "market_closed"
    assert v["severity"] == "ok"


def test_never_scanned_is_idle_warning():
    v = evaluate(
        now=NOW,
        last_scan_time=None,
        last_nonzero_scan_time=None,
        cadence_minutes=15,
        market_open=True,
    )
    assert v["status"] == "idle"


def test_stall_takes_priority_over_starve():
    # Both stalled and starved → the critical (dead-loop) verdict wins.
    v = evaluate(
        now=NOW,
        last_scan_time=_t(hours_ago=3),
        last_nonzero_scan_time=_t(hours_ago=20),
        cadence_minutes=15,
        market_open=True,
    )
    assert v["status"] == "stalled"


def test_zero_signals_but_recent_is_still_ok_within_starve_window():
    # 0 signals for only 2h is normal quiet — not yet starved.
    v = evaluate(
        now=NOW,
        last_scan_time=_t(minutes_ago=2),
        last_nonzero_scan_time=_t(hours_ago=2),
        cadence_minutes=15,
        market_open=True,
    )
    assert v["status"] == "ok"

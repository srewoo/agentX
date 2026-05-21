"""Smoke tests for the weekly-cadence helpers.

Verifies Monday 10:30 IST (backtest) and Friday 16:30 IST (calibration)
schedule correctly across week boundaries and same-day-after-cutoff cases.
IST is UTC+5:30 so 10:30 IST = 05:00 UTC and 16:30 IST = 11:00 UTC.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.services.orchestrator import (
    _next_weekly_at,
    _next_weekly_backtest_dt,
    _next_weekly_calibration_dt,
)

IST = timezone(timedelta(hours=5, minutes=30))


def _at_ist(year, month, day, hour, minute):
    """Build a UTC datetime corresponding to the given IST clock time."""
    return datetime(year, month, day, hour, minute, tzinfo=IST).astimezone(timezone.utc)


@pytest.mark.parametrize("now_ist,expected_weekday,expected_hour_ist", [
    # Tue (weekday=1) → next Monday (weekday=0)
    (datetime(2026, 5, 19, 12, 0, tzinfo=IST), 0, 10),
    # Sun (weekday=6) → next-day Monday
    (datetime(2026, 5, 24, 23, 0, tzinfo=IST), 0, 10),
    # Mon BEFORE 10:30 IST → same day
    (datetime(2026, 5, 18, 9, 0, tzinfo=IST), 0, 10),
    # Mon AFTER 10:30 IST → next Monday
    (datetime(2026, 5, 18, 11, 0, tzinfo=IST), 0, 10),
])
def test_next_weekly_backtest_lands_on_monday_1030_ist(now_ist, expected_weekday, expected_hour_ist):
    with patch("app.services.orchestrator.datetime") as dt_mock:
        dt_mock.now.return_value = now_ist.astimezone(timezone.utc)
        # Pass through the datetime constructor + timedelta usage in helper
        dt_mock.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = _next_weekly_backtest_dt()
    result_ist = result.astimezone(IST)
    assert result_ist.weekday() == expected_weekday
    assert (result_ist.hour, result_ist.minute) == (10, 30)
    # Strictly in the future relative to now (or same-instant if exactly aligned).
    assert result_ist >= now_ist


@pytest.mark.parametrize("now_ist", [
    datetime(2026, 5, 19, 12, 0, tzinfo=IST),     # Tue
    datetime(2026, 5, 22, 16, 29, tzinfo=IST),    # Fri 1 minute before
    datetime(2026, 5, 22, 17, 0, tzinfo=IST),     # Fri 30 min after → next Fri
    datetime(2026, 5, 24, 23, 0, tzinfo=IST),     # Sun
])
def test_next_weekly_calibration_lands_on_friday_1630_ist(now_ist):
    with patch("app.services.orchestrator.datetime") as dt_mock:
        dt_mock.now.return_value = now_ist.astimezone(timezone.utc)
        dt_mock.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = _next_weekly_calibration_dt()
    result_ist = result.astimezone(IST)
    assert result_ist.weekday() == 4  # Friday
    assert (result_ist.hour, result_ist.minute) == (16, 30)
    assert result_ist >= now_ist


def test_next_weekly_at_wraps_when_same_weekday_already_past():
    """Same weekday but past the cutoff time should jump 7 days."""
    # Friday 17:00 IST — past 16:30 cutoff
    now_ist = datetime(2026, 5, 22, 17, 0, tzinfo=IST)
    with patch("app.services.orchestrator.datetime") as dt_mock:
        dt_mock.now.return_value = now_ist.astimezone(timezone.utc)
        dt_mock.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = _next_weekly_at(weekday=4, hour=16, minute=30)
    result_ist = result.astimezone(IST)
    delta = result_ist - now_ist
    # Should be ~7 days minus 30 minutes (next Friday at 16:30).
    assert 6 <= delta.days <= 7
    assert result_ist.weekday() == 4

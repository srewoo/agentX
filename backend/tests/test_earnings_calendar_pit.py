from __future__ import annotations
"""3.4 — point-in-time historical earnings blackout."""
from datetime import date

from app.services import earnings_calendar_pit as ec


def test_blackout_when_earnings_imminent():
    earnings = [date(2024, 4, 18)]
    # 3 days before earnings → within the 5-day upcoming window → blackout.
    assert ec.is_in_blackout(earnings, date(2024, 4, 15)) is True
    # 10 days before → outside the window → no blackout.
    assert ec.is_in_blackout(earnings, date(2024, 4, 8)) is False


def test_past_earnings_do_not_blackout():
    # Results already happened yesterday → PEAD window, live engine does NOT
    # sit this out, so neither does the backtest.
    assert ec.is_in_blackout([date(2024, 4, 18)], date(2024, 4, 19)) is False


def test_exact_asof_day_is_not_blackout_but_within_window_is():
    earnings = [date(2024, 4, 20)]
    assert ec.is_in_blackout(earnings, date(2024, 4, 20)) is False   # asof == earnings (not > asof)
    assert ec.is_in_blackout(earnings, date(2024, 4, 16)) is True    # 4 days ahead


def test_empty_calendar_is_inert():
    assert ec.is_in_blackout([], date(2024, 4, 15)) is False


def test_window_is_configurable():
    earnings = [date(2024, 4, 18)]
    assert ec.is_in_blackout(earnings, date(2024, 4, 12), window_days=3) is False  # 6 days out
    assert ec.is_in_blackout(earnings, date(2024, 4, 12), window_days=7) is True


def test_is_in_blackout_at_inert_without_calendar(monkeypatch):
    monkeypatch.setattr(ec, "_load_calendar", lambda: {})
    ec._reset_cache()
    assert ec.is_in_blackout_at("INFY", date(2024, 4, 15)) is False


def test_is_in_blackout_at_uses_calendar(monkeypatch):
    monkeypatch.setattr(ec, "_load_calendar", lambda: {"INFY": [date(2024, 4, 18)]})
    assert ec.is_in_blackout_at("infy", date(2024, 4, 15)) is True   # case-insensitive
    assert ec.is_in_blackout_at("TCS", date(2024, 4, 15)) is False   # not in calendar

from __future__ import annotations
"""Tests for the point-in-time data-quality layer.

The contract that matters: a fundamental figure is *never* visible before it
was public (no look-ahead), and the survivorship-free universe seam returns
point-in-time members when history exists and honestly flags the bias when it
doesn't.
"""
from datetime import date

from app.services.fundamentals_pit import select_asof, _filing_date, DEFAULT_LAG_DAYS
from app.services import universe_pit


# ── Statements: Q ending dates with realistic Indian filing lags ──
STMTS = [
    {"date": "2020-12-31", "fillingDate": "2021-02-10", "peRatio": 18.0, "roe": 0.15},
    {"date": "2021-03-31", "fillingDate": "2021-05-12", "peRatio": 20.0, "roe": 0.17},
    {"date": "2021-06-30", "fillingDate": "2021-08-09", "peRatio": 22.0, "roe": 0.18},
]


class TestSelectAsof:
    def test_returns_latest_public_statement(self):
        # On 2021-06-01, only the two statements filed by then are public.
        row = select_asof(STMTS, date(2021, 6, 1))
        assert row is not None
        assert row["date"] == "2021-03-31"  # filed 2021-05-12, public; Q2 not filed till Aug

    def test_no_lookahead_on_filing_day_within_lag(self):
        # The Q2 statement files 2021-08-09. With the default 1-day lag it is
        # NOT yet usable on the filing day itself.
        row = select_asof(STMTS, date(2021, 8, 9), lag_days=DEFAULT_LAG_DAYS)
        assert row["date"] == "2021-03-31"  # still the prior one
        # One day after the lag clears, it becomes visible.
        row2 = select_asof(STMTS, date(2021, 8, 11), lag_days=DEFAULT_LAG_DAYS)
        assert row2["date"] == "2021-06-30"

    def test_returns_none_before_first_filing(self):
        # Backtesting a date before anything was filed → no figure at all.
        assert select_asof(STMTS, date(2020, 1, 1)) is None

    def test_order_independent(self):
        # Selection is by filing date, not list order.
        shuffled = [STMTS[2], STMTS[0], STMTS[1]]
        row = select_asof(shuffled, date(2021, 6, 1))
        assert row["date"] == "2021-03-31"

    def test_falls_back_to_period_date_when_no_filing_date(self):
        rows = [{"date": "2021-03-31", "peRatio": 20.0}]  # no fillingDate
        # period date used as weak proxy; public well after period end.
        assert select_asof(rows, date(2021, 12, 1)) is not None
        assert select_asof(rows, date(2021, 1, 1)) is None

    def test_filing_date_prefers_filling_over_accepted_over_period(self):
        assert _filing_date({"fillingDate": "2021-05-12", "acceptedDate": "2021-05-15", "date": "2021-03-31"}) == date(2021, 5, 12)
        assert _filing_date({"acceptedDate": "2021-05-15", "date": "2021-03-31"}) == date(2021, 5, 15)
        assert _filing_date({"date": "2021-03-31"}) == date(2021, 3, 31)


HISTORY = [
    {"symbol": "ALIVE",   "added": date(2018, 1, 1), "removed": None},
    {"symbol": "GONE",    "added": date(2018, 1, 1), "removed": date(2022, 6, 1)},
    {"symbol": "NEWADD",  "added": date(2023, 1, 1), "removed": None},
    {"symbol": "FOREVER", "added": None,             "removed": None},
]


class TestMembersAt:
    def test_includes_delisted_name_before_removal(self):
        # The whole point: GONE was a real member in 2021 and must be in the
        # universe for that date even though it's delisted today.
        members = universe_pit.members_at(HISTORY, date(2021, 1, 1))
        assert "GONE" in members
        assert "ALIVE" in members
        assert "FOREVER" in members
        assert "NEWADD" not in members  # not added yet

    def test_excludes_after_removal(self):
        members = universe_pit.members_at(HISTORY, date(2023, 1, 1))
        assert "GONE" not in members      # delisted 2022-06
        assert "NEWADD" in members        # added 2023-01

    def test_blank_added_is_always_member_lower_bound(self):
        assert "FOREVER" in universe_pit.members_at(HISTORY, date(2010, 1, 1))


class TestUniverseSeam:
    def test_fallback_flags_survivorship_when_no_history(self, monkeypatch):
        # No CSV present → today's static list, survivorship_free False.
        monkeypatch.setattr(universe_pit, "has_constituent_history", lambda: False)
        syms, free = universe_pit.get_universe_at_date("2021-06-30", limit=10)
        assert free is False
        assert len(syms) <= 10 and len(syms) > 0

    def test_uses_history_when_present(self, monkeypatch):
        monkeypatch.setattr(universe_pit, "has_constituent_history", lambda: True)
        monkeypatch.setattr(universe_pit, "_load_history", lambda: HISTORY)
        syms, free = universe_pit.get_universe_at_date("2021-01-01")
        assert free is True
        assert "GONE" in syms  # survivorship-free: includes the delisted name

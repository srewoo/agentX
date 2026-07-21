from __future__ import annotations
"""3.2 — survivorship-free universe: membership math + completeness guard."""
from datetime import date

from app.services import universe_pit as up


def test_members_at_respects_add_remove_windows():
    rows = [
        {"symbol": "A", "added": date(2020, 1, 1), "removed": None},        # still in
        {"symbol": "B", "added": None, "removed": date(2023, 7, 12)},       # left mid-window
        {"symbol": "C", "added": date(2024, 1, 1), "removed": None},        # joined later
    ]
    # As of 2022: A (in), B (still in, leaves 2023), C not yet.
    assert set(up.members_at(rows, date(2022, 6, 1))) == {"A", "B"}
    # As of 2024-06: A + C; B has left.
    assert set(up.members_at(rows, date(2024, 6, 1))) == {"A", "C"}


def test_sparse_or_snapshot_csv_not_trusted_as_survivorship_free(monkeypatch, tmp_path):
    # A "snapshot" CSV: 50 current members, NO removals → must NOT be trusted
    # (it would apply today's index to the past). Guard → survivorship_free=False.
    rows = [{"symbol": f"S{i}", "added": None, "removed": None} for i in range(50)]
    monkeypatch.setattr(up, "has_constituent_history", lambda: True)
    monkeypatch.setattr(up, "_load_history", lambda: rows)
    syms, sf = up.get_universe_at_date(date(2022, 1, 1))
    assert sf is False    # no turnover → refused despite 50 members


def test_history_with_turnover_is_trusted(monkeypatch):
    # Real turnover (a `removed` date) + enough members → trusted survivorship-free.
    rows = [{"symbol": f"S{i}", "added": None, "removed": None} for i in range(10)]
    rows.append({"symbol": "GONE", "added": None, "removed": date(2021, 1, 1)})
    monkeypatch.setattr(up, "has_constituent_history", lambda: True)
    monkeypatch.setattr(up, "_load_history", lambda: rows)
    syms, sf = up.get_universe_at_date(date(2022, 1, 1))
    assert sf is True
    assert "GONE" not in syms       # left before asof


def test_near_empty_history_falls_back(monkeypatch):
    # Below the sanity floor at the asof date → fall back honestly.
    rows = [{"symbol": "A", "added": None, "removed": date(2021, 1, 1)}]  # 0 members at 2022
    monkeypatch.setattr(up, "has_constituent_history", lambda: True)
    monkeypatch.setattr(up, "_load_history", lambda: rows)
    _, sf = up.get_universe_at_date(date(2022, 1, 1))
    assert sf is False

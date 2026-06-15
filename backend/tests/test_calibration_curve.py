from __future__ import annotations
"""C1 — conviction calibration curve.

Unit-tests the math (isotonic monotonicity, Brier, reliability bins) and the
DB round-trip (build → persist → load → apply), including the insufficient-
data refusal.
"""
import os
import sqlite3
import tempfile

import pytest

from app.services import calibration_curve as cc


def test_isotonic_fit_is_monotone_non_decreasing():
    xs = [10, 20, 30, 40, 50, 60, 70, 80, 90]
    # Deliberately non-monotone realized rates → PAV must smooth them.
    ys = [0, 0, 1, 0, 1, 1, 0, 1, 1]
    curve = cc.isotonic_fit(xs, ys)
    vals = [v for _, v in curve]
    assert vals == sorted(vals)  # non-decreasing
    assert all(0.0 <= v <= 1.0 for v in vals)


def test_apply_curve_clamps_and_steps():
    curve = cc.isotonic_fit([20, 40, 60, 80], [0, 0, 1, 1])
    # Below the lowest breakpoint → lowest fitted value; above highest → highest.
    assert cc.apply_curve(10, curve) <= cc.apply_curve(90, curve)
    assert 0.0 <= cc.apply_curve(50, curve) <= 1.0


def test_apply_curve_empty_is_identity():
    assert cc.apply_curve(70, []) == 0.70


def test_brier_score_perfect_is_zero():
    assert cc.brier_score([1.0, 0.0, 1.0], [1, 0, 1]) == 0.0
    assert cc.brier_score([0.5, 0.5], [1, 0]) == 0.25


def test_reliability_bins_partition_counts():
    convictions = [15, 25, 75, 85, 85]
    labels = [0, 1, 1, 1, 0]
    bins = cc.reliability_bins(convictions, labels, n_bins=10)
    assert sum(b["n"] for b in bins) == len(convictions)
    for b in bins:
        assert 0.0 <= b["realized"] <= 1.0


@pytest.fixture
def outcomes_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE recommendation_outcomes (
        rec_id TEXT PRIMARY KEY, symbol TEXT, horizon TEXT, action TEXT,
        conviction INTEGER, outcome TEXT)""")
    con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.commit()
    con.close()
    yield path
    os.unlink(path)


def _seed(path, rows):
    con = sqlite3.connect(path)
    for i, (conv, outcome) in enumerate(rows):
        con.execute(
            "INSERT INTO recommendation_outcomes(rec_id,symbol,horizon,action,conviction,outcome)"
            " VALUES (?,?,?,?,?,?)",
            (f"r{i}", "INFY", "swing", "BUY", conv, outcome),
        )
    con.commit()
    con.close()


@pytest.mark.asyncio
async def test_build_refuses_insufficient_data(outcomes_db):
    _seed(outcomes_db, [(70, "win"), (60, "loss")])
    report = await cc.build_calibration_curve(db_path=outcomes_db)
    assert report["status"] == "insufficient_data"
    assert await cc.get_calibration_curve(db_path=outcomes_db) is None


@pytest.mark.asyncio
async def test_build_persists_and_calibrates(outcomes_db):
    # 120 trades where high conviction really does win more → curve should
    # separate low from high and beat (or match) raw Brier.
    rows = []
    for _ in range(60):
        rows.append((30, "loss"))   # low conviction, loses
    for _ in range(40):
        rows.append((80, "win"))    # high conviction, wins
    for _ in range(20):
        rows.append((80, "loss"))   # some high-conviction losses (realism)
    _seed(outcomes_db, rows)

    report = await cc.build_calibration_curve(db_path=outcomes_db)
    assert report["status"] == "ok"
    assert report["samples"] == 120
    # Calibration must not be worse than raw conviction-as-probability.
    assert report["brier_calibrated"] <= report["brier_raw"] + 1e-9

    # Round-trips and the accessor returns a sane probability.
    loaded = await cc.get_calibration_curve(db_path=outcomes_db)
    assert loaded and loaded["status"] == "ok"
    p_low = await cc.calibrated_win_prob(30, db_path=outcomes_db)
    p_high = await cc.calibrated_win_prob(80, db_path=outcomes_db)
    assert p_low is not None and p_high is not None
    assert p_high >= p_low  # monotone: more conviction → not-lower p(win)


@pytest.mark.asyncio
async def test_calibrated_win_prob_none_when_not_built(outcomes_db):
    assert await cc.calibrated_win_prob(70, db_path=outcomes_db) is None

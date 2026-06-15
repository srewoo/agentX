from __future__ import annotations
"""D2–D4 — forward reporting, readiness, durability."""
import os
import sqlite3
import tempfile

import pytest

from app.database import CREATE_PAPER_TRADES_TABLE
from app.services import forward_report as fr


# ── pure stats ──
def test_wilson_interval_widens_with_small_n():
    lo_small, hi_small = fr.wilson_interval(6, 10)
    lo_big, hi_big = fr.wilson_interval(600, 1000)
    assert (hi_small - lo_small) > (hi_big - lo_big)


def test_sharpe_and_expectancy():
    assert fr.expectancy([1.0, -1.0, 2.0]) == pytest.approx(0.6667, abs=1e-3)
    assert fr.sharpe([1, 1, 1]) == 0.0          # zero variance
    assert fr.sharpe([1.0]) == 0.0              # too few points


def test_max_drawdown():
    # 100 -> 120 -> 90 : peak 120, trough 90 → 25%
    assert fr.max_drawdown([100, 120, 90, 110]) == 25.0


def test_readiness_gate():
    assert fr.readiness(50, target=300)["ready"] is False
    assert fr.readiness(300, target=300)["ready"] is True
    assert "insufficient" in fr.readiness(9, target=300)["message"]


def test_durability_detects_divergence():
    # Forward 35/100 = 35% with CI nowhere near a backtest claim of 50%.
    v = fr.durability_verdict(35, 100, backtest_win_rate=0.50)
    assert v["diverged"] is True
    # Forward 48/100 overlaps a 50% backtest claim → consistent.
    v2 = fr.durability_verdict(48, 100, backtest_win_rate=0.50)
    assert v2["diverged"] is False


# ── DB aggregator ──
@pytest.fixture
def trades_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute(CREATE_PAPER_TRADES_TABLE)
    con.commit()
    con.close()
    yield path
    os.unlink(path)


def _close(path, rows):
    con = sqlite3.connect(path)
    for i, (pnl_pct, pnl_amt) in enumerate(rows):
        con.execute(
            "INSERT INTO paper_trades(trade_id,symbol,direction,signal_type,strength,"
            "entry_price,entry_date,status,pnl_pct,pnl_amount,exit_date) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"t{i}", "INFY", "bullish", "x", 5, 100, "2026-06-01", "closed",
             pnl_pct, pnl_amt, f"2026-06-{2+i:02d}"),
        )
    con.commit()
    con.close()


@pytest.mark.asyncio
async def test_forward_performance_reports_metrics(trades_db):
    _close(trades_db, [(5.0, 5000), (-2.0, -2000), (3.0, 3000), (-1.0, -1000)])
    rep = await fr.forward_performance(db_path=trades_db, capital=100_000,
                                       benchmark_return_pct=1.0, target_trades=300)
    assert rep["trades"] == 4
    assert rep["wins"] == 2
    assert rep["win_rate"] == 0.5
    assert rep["readiness"]["ready"] is False        # 4 << 300
    assert rep["alpha_pct"] == pytest.approx(rep["expectancy_pct"] - 1.0, abs=1e-6)
    assert len(rep["win_rate_ci"]) == 2


@pytest.mark.asyncio
async def test_forward_performance_empty(trades_db):
    rep = await fr.forward_performance(db_path=trades_db)
    assert rep["trades"] == 0
    assert rep["readiness"]["ready"] is False


@pytest.mark.asyncio
async def test_durability_check_reads_db(trades_db):
    _close(trades_db, [(-1, -100)] * 7 + [(2, 200)] * 3)  # 30% win rate
    v = await fr.durability_check(db_path=trades_db, backtest_win_rate=0.50)
    assert v["forward_win_rate"] == 0.3

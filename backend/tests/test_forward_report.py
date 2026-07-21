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


def test_expectancy_lower_bound():
    # LB is strictly below the point estimate when there is dispersion.
    pnls = [1.0, -1.0, 2.0, 0.5, -0.5, 1.5]
    assert fr.expectancy_lower_bound(pnls) < fr.expectancy(pnls)
    # A tight, clearly-positive sample keeps its LB above zero.
    assert fr.expectancy_lower_bound([1.0, 1.1, 0.9, 1.05, 0.95] * 10) > 0
    # A noisy small sample around zero has a LB below zero (not "proven").
    assert fr.expectancy_lower_bound([5.0, -5.0, 4.0, -4.0]) < 0
    # Degenerate sizes return 0.0 (no dispersion estimate).
    assert fr.expectancy_lower_bound([]) == 0.0
    assert fr.expectancy_lower_bound([3.0]) == 0.0


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


# ── regime-stratified verdict (4.4) ──
def test_classify_regime():
    assert fr.classify_regime(5.0) == "trend_up"
    assert fr.classify_regime(-4.0) == "trend_down"
    assert fr.classify_regime(1.0) == "sideways"
    assert fr.classify_regime(3.0) == "trend_up"      # boundary inclusive


def test_regime_stratified_splits_edge_by_regime():
    trades = [
        {"regime": "trend_up", "pnl_pct": 5.0},
        {"regime": "trend_up", "pnl_pct": 3.0},
        {"regime": "trend_up", "pnl_pct": 2.0},
        {"regime": "trend_down", "pnl_pct": -4.0},
        {"regime": "trend_down", "pnl_pct": -2.0},
    ]
    out = fr.regime_stratified(trades)
    assert out["trend_up"]["n"] == 3 and out["trend_up"]["wins"] == 3
    assert out["trend_up"]["win_rate"] == 1.0
    assert out["trend_down"]["wins"] == 0        # edge only exists in trend_up
    assert out["trend_down"]["expectancy_pct"] < 0


def test_regime_stratified_unknown_bucket():
    out = fr.regime_stratified([{"pnl_pct": 1.0}, {"pnl_pct": -1.0}])
    assert out["unknown"]["n"] == 2


# ── book beta vs benchmark (4.2) ──
def test_beta_perfectly_correlated_book():
    # trade returns == index returns → beta 1.0.
    rets = [1.0, -2.0, 3.0, -1.0, 0.5]
    assert fr.beta(rets, rets) == pytest.approx(1.0, abs=1e-6)


def test_beta_double_exposure():
    bench = [1.0, -2.0, 3.0, -1.0]
    trade = [2.0, -4.0, 6.0, -2.0]     # 2x the index → beta 2.0
    assert fr.beta(trade, bench) == pytest.approx(2.0, abs=1e-6)


def test_beta_undefined_returns_none():
    assert fr.beta([1.0], [1.0]) is None          # n < 2
    assert fr.beta([1.0, 2.0], [3.0, 3.0]) is None  # no benchmark variance


# ── forward-window integrity ──
def test_window_integrity_counts_gaps():
    rows = [
        {"date": "2026-06-24", "status": None},        # pre-migration → ok
        {"date": "2026-06-25", "status": "ok"},
        {"date": "2026-06-26", "status": "MISSED_RUN"},
        {"date": "2026-06-29", "status": "MISSED_RUN"},
    ]
    integ = fr.window_integrity(rows)
    assert integ["logged_days"] == 2
    assert integ["missed_runs"] == 2
    assert integ["missed_dates"] == ["2026-06-26", "2026-06-29"]
    assert integ["clean_fraction"] == 0.5
    assert integ["degraded"] is True


def test_window_integrity_clean_when_no_gaps():
    integ = fr.window_integrity([{"date": "2026-06-24", "status": "ok"}])
    assert integ["degraded"] is False
    assert integ["clean_fraction"] == 1.0
    assert fr.window_integrity([])["clean_fraction"] == 1.0


# ── per-trade benchmark attribution ──
def _bench_series(prices):
    import pandas as pd
    idx = pd.date_range("2026-06-01", periods=len(prices), freq="D")
    return pd.Series(prices, index=idx)


def test_attach_benchmark_excess_long_direction_aware():
    # NIFTY +10% over the window (100→110). A long +15% pnl beats the index by 5.
    bench = _bench_series([100, 102, 104, 106, 108, 110])
    trades = [{
        "direction": "bullish", "pnl_pct": 15.0,
        "entry_date": "2026-06-01", "exit_date": "2026-06-06",
    }]
    stamped = fr.attach_benchmark_excess(trades, bench)
    assert stamped == 1
    assert trades[0]["bench_ret"] == pytest.approx(10.0, abs=1e-6)
    assert trades[0]["excess_pnl"] == pytest.approx(5.0, abs=1e-6)


def test_attach_benchmark_excess_short_adds_index_move():
    # Index +10% but a short still made +3% pnl → alpha = 3 + 10 = 13 (it fought the tape).
    bench = _bench_series([100, 102, 104, 106, 108, 110])
    trades = [{
        "direction": "bearish", "pnl_pct": 3.0,
        "entry_date": "2026-06-01", "exit_date": "2026-06-06",
    }]
    fr.attach_benchmark_excess(trades, bench)
    assert trades[0]["excess_pnl"] == pytest.approx(13.0, abs=1e-6)


def test_attach_benchmark_excess_fail_open():
    assert fr.attach_benchmark_excess([], _bench_series([100, 101])) == 0
    assert fr.attach_benchmark_excess([{"pnl_pct": 1.0}], None) == 0
    # Unresolvable window (exit before any bar) leaves the trade un-stamped.
    bench = _bench_series([100, 101, 102])
    trades = [{"direction": "bullish", "pnl_pct": 1.0,
               "entry_date": "2020-01-01", "exit_date": "2020-01-02"}]
    assert fr.attach_benchmark_excess(trades, bench) == 0
    assert "excess_pnl" not in trades[0]


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
async def test_live_combo_records_groups_by_signal_direction(trades_db):
    con = sqlite3.connect(trades_db)
    rows = [
        ("macd", "bullish", 5.0), ("macd", "bullish", -2.0), ("macd", "bullish", 3.0),
        ("rsi", "bearish", -1.0),
        ("noise", "neutral", 4.0),  # excluded
    ]
    for i, (st, direction, pnl) in enumerate(rows):
        con.execute(
            "INSERT INTO paper_trades(trade_id,symbol,direction,signal_type,strength,"
            "entry_price,entry_date,status,pnl_pct,pnl_amount,exit_date) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"t{i}", "INFY", direction, st, 5, 100, "2026-06-01", "closed",
             pnl, pnl * 100, "2026-06-05"),
        )
    con.commit(); con.close()
    recs = await fr.live_combo_records(db_path=trades_db)
    assert recs["macd|bullish"] == (2, 3)   # 2 wins of 3
    assert recs["rsi|bearish"] == (0, 1)
    assert "noise|neutral" not in recs      # neutral excluded


@pytest.mark.asyncio
async def test_durability_check_reads_db(trades_db):
    _close(trades_db, [(-1, -100)] * 7 + [(2, 200)] * 3)  # 30% win rate
    v = await fr.durability_check(db_path=trades_db, backtest_win_rate=0.50)
    assert v["forward_win_rate"] == 0.3

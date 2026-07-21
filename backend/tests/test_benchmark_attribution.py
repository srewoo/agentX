"""Benchmark (NIFTY) attribution in the walk-forward backtester."""
from __future__ import annotations

import pandas as pd

from app.services.backtester_walk_forward import _attach_benchmark, _fold_metrics


def _bench_series(daily_ret_pct: float, n: int = 30):
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    vals = [100.0 * (1 + daily_ret_pct / 100.0) ** i for i in range(n)]
    return pd.Series(vals, index=idx)


def _sym_index(n: int = 30):
    return pd.date_range("2024-01-01", periods=n, freq="D")


def test_bullish_excess_subtracts_index_return():
    # Market up 1%/day; a bullish trade making 5% over 5 bars has ~0% alpha.
    bench = _bench_series(1.0)
    trades = [{
        "bar_index": 2, "direction": "bullish",
        "pnl_5d": 5.101, "bars_held_5d": 5,
    }]
    _attach_benchmark(trades, _sym_index(), bench, [5])
    t = trades[0]
    assert abs(t["bench_ret_5d"] - 5.101) < 0.01
    assert abs(t["excess_pnl_5d"]) < 0.02  # rode the tape, no alpha


def test_bearish_excess_adds_index_move_it_fought():
    # Market up 1%/day; a short that still made +2% fought a +5.1% tape —
    # that's real alpha of roughly +7.1%.
    bench = _bench_series(1.0)
    trades = [{
        "bar_index": 2, "direction": "bearish",
        "pnl_5d": 2.0, "bars_held_5d": 5,
    }]
    _attach_benchmark(trades, _sym_index(), bench, [5])
    assert trades[0]["excess_pnl_5d"] > 7.0


def test_missing_benchmark_attaches_nothing():
    trades = [{"bar_index": 2, "direction": "bullish", "pnl_5d": 5.0}]
    _attach_benchmark(trades, _sym_index(), None, [5])
    assert "excess_pnl_5d" not in trades[0]
    assert "bench_ret_5d" not in trades[0]


def test_fold_metrics_aggregates_excess_when_present():
    trades = [
        {"pnl_5d": 3.0, "win_5d": True, "excess_pnl_5d": 1.0, "bench_ret_5d": 2.0},
        {"pnl_5d": -1.0, "win_5d": False, "excess_pnl_5d": -3.0, "bench_ret_5d": 2.0},
    ]
    m = _fold_metrics(trades, [5])
    assert m["excess_avg_pnl_5d"] == -1.0
    assert m["bench_avg_ret_5d"] == 2.0
    assert m["excess_positive_5d"] == 50.0


def test_fold_metrics_omits_excess_keys_when_benchmark_missing():
    trades = [{"pnl_5d": 3.0, "win_5d": True}]
    m = _fold_metrics(trades, [5])
    assert "excess_avg_pnl_5d" not in m

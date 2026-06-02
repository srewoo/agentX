from __future__ import annotations
"""Tests for app.services.performance_metrics — audited edge metrics."""
import math

import pytest

from app.services.performance_metrics import (
    compute_metrics,
    group_metrics,
    max_drawdown_pp,
)


class TestMaxDrawdown:
    def test_monotonic_up_has_no_drawdown(self):
        assert max_drawdown_pp([1.0, 2.0, 3.0]) == 0.0

    def test_simple_drawdown(self):
        # cumulative: 5, 3, 6 → peak 5 then dip to 3 → drawdown 2
        assert max_drawdown_pp([5.0, -2.0, 3.0]) == 2.0

    def test_worst_of_multiple_dips(self):
        # cum: 10, 6, 12, 4 → dips 4 then 8 → worst 8
        assert max_drawdown_pp([10.0, -4.0, 6.0, -8.0]) == 8.0


class TestComputeMetrics:
    def test_empty_is_safe(self):
        m = compute_metrics([])
        assert m["n_resolved"] == 0
        assert m["profit_factor"] is None
        assert m["expectancy"] == 0.0

    def test_profit_factor_and_expectancy(self):
        # 2 wins of +2, +4; 2 losses of -1, -1 → PF = 6/2 = 3.0, exp = 1.0
        trades = [{"pnl_pct": 2.0}, {"pnl_pct": 4.0}, {"pnl_pct": -1.0}, {"pnl_pct": -1.0}]
        m = compute_metrics(trades, annualise=False)
        assert m["wins"] == 2 and m["losses"] == 2
        assert m["hit_rate"] == 50.0
        assert m["profit_factor"] == pytest.approx(3.0)
        assert m["expectancy"] == pytest.approx(1.0)
        assert m["avg_win"] == pytest.approx(3.0)
        assert m["avg_loss"] == pytest.approx(-1.0)
        assert m["payoff_ratio"] == pytest.approx(3.0)

    def test_profit_factor_none_when_no_losses(self):
        m = compute_metrics([{"pnl_pct": 1.0}, {"pnl_pct": 2.0}], annualise=False)
        assert m["profit_factor"] is None  # undefined: no downside
        assert m["hit_rate"] == 100.0

    def test_low_winrate_can_still_be_profitable(self):
        # The core thesis: 40% WR with 3:1 payoff is +EV.
        trades = [{"pnl_pct": 3.0}] * 4 + [{"pnl_pct": -1.0}] * 6
        m = compute_metrics(trades, annualise=False)
        assert m["hit_rate"] == 40.0
        assert m["expectancy"] > 0  # +0.6
        assert m["profit_factor"] == pytest.approx(2.0)

    def test_sharpe_annualised_uses_hold_days(self):
        trades = [{"pnl_pct": 1.0, "hold_days": 5}, {"pnl_pct": 2.0, "hold_days": 5},
                  {"pnl_pct": -0.5, "hold_days": 5}]
        m = compute_metrics(trades, annualise=True, trading_days=252)
        assert m["sharpe_per_trade"] != 0.0
        assert m["sharpe_annualised"] is not None
        # annualised = per_trade * sqrt(252/5)
        assert m["sharpe_annualised"] == pytest.approx(
            m["sharpe_per_trade"] * math.sqrt(252 / 5), abs=1e-3
        )

    def test_brier_perfect_calibration(self):
        # Predict 1.0 on a win and 0.0 on a loss → Brier 0.
        trades = [
            {"pnl_pct": 2.0, "outcome": "win", "predicted_prob": 1.0},
            {"pnl_pct": -1.0, "outcome": "loss", "predicted_prob": 0.0},
        ]
        m = compute_metrics(trades, annualise=False)
        assert m["brier_score"] == 0.0

    def test_brier_worst_calibration(self):
        # Predict 1.0 on a loss and 0.0 on a win → Brier 1.0.
        trades = [
            {"pnl_pct": -1.0, "outcome": "loss", "predicted_prob": 1.0},
            {"pnl_pct": 2.0, "outcome": "win", "predicted_prob": 0.0},
        ]
        m = compute_metrics(trades, annualise=False)
        assert m["brier_score"] == 1.0

    def test_calibration_curve_bins(self):
        trades = [
            {"pnl_pct": 1.0, "outcome": "win", "predicted_prob": 0.95},
            {"pnl_pct": -1.0, "outcome": "loss", "predicted_prob": 0.92},
            {"pnl_pct": 1.0, "outcome": "win", "predicted_prob": 0.15},
        ]
        m = compute_metrics(trades, annualise=False)
        # Two bins populated: 0.1-0.2 and 0.9-1.0.
        labels = {b["bin"] for b in m["calibration"]}
        assert "0.9-1.0" in labels
        hi = next(b for b in m["calibration"] if b["bin"] == "0.9-1.0")
        assert hi["count"] == 2
        assert hi["observed_win_rate"] == pytest.approx(0.5)


class TestGroupMetrics:
    def test_groups_by_key(self):
        trades = [
            {"pnl_pct": 2.0, "regime": "trend_up"},
            {"pnl_pct": -1.0, "regime": "trend_up"},
            {"pnl_pct": -3.0, "regime": "panic"},
        ]
        g = group_metrics(trades, key=lambda t: t.get("regime"), annualise=False)
        assert set(g.keys()) == {"trend_up", "panic"}
        assert g["panic"]["expectancy"] == pytest.approx(-3.0)
        assert g["trend_up"]["n_resolved"] == 2

    def test_none_key_skips(self):
        trades = [{"pnl_pct": 1.0, "regime": None}, {"pnl_pct": 2.0, "regime": "x"}]
        g = group_metrics(trades, key=lambda t: t.get("regime"), annualise=False)
        assert set(g.keys()) == {"x"}

from __future__ import annotations
"""Regression test: size-aware sqrt-impact cost actually reduces P&L.

Before the fix the Almgren-Chriss `sqrt_impact_cost_bps` was computed in
walk-forward and shelved — headline P&L used only the flat ADV-bucket
slippage. `apply_costs` now accepts `extra_slippage_pct` so the impact term
is injected into net P&L, and `_evaluate_outcome_realistic` threads it.
"""
from app.services.execution_costs import apply_costs, round_trip_cost_pct, sqrt_impact_cost_bps
from app.services.backtester import _evaluate_outcome_realistic


def test_extra_slippage_default_is_no_op():
    base = apply_costs(entry=100.0, exit=110.0, qty=100, segment="cash")
    same = apply_costs(entry=100.0, exit=110.0, qty=100, segment="cash",
                       extra_slippage_pct=0.0)
    assert base["net_pnl"] == same["net_pnl"]
    assert "market_impact" not in base["breakdown"]


def test_extra_slippage_reduces_net_pnl():
    base = apply_costs(entry=100.0, exit=110.0, qty=100, segment="cash")
    with_impact = apply_costs(entry=100.0, exit=110.0, qty=100, segment="cash",
                              extra_slippage_pct=0.5)  # 0.5% round-trip impact
    # 0.5% of buy notional (100*100=10,000) = ₹50 extra cost.
    assert with_impact["breakdown"]["market_impact"] == 50.0
    assert with_impact["net_pnl"] == base["net_pnl"] - 50.0


def test_evaluate_outcome_realistic_applies_impact():
    no_impact = _evaluate_outcome_realistic(
        "bullish", 100.0, 110.0, qty=1, apply_slippage=False)
    with_impact = _evaluate_outcome_realistic(
        "bullish", 100.0, 110.0, qty=1, apply_slippage=False,
        extra_slippage_pct=0.4)
    assert with_impact["pnl_pct"] < no_impact["pnl_pct"]
    # The gap is ~0.4pp (the injected round-trip impact on entry notional).
    assert abs((no_impact["pnl_pct"] - with_impact["pnl_pct"]) - 0.4) < 0.05


def test_impact_scales_with_participation():
    """A larger order (higher participation) pays more impact — the property
    the flat bucket slippage cannot express."""
    small = sqrt_impact_cost_bps(
        trade_value_inr=1e5, avg_daily_value_inr=1e9, daily_vol_pct=2.0)["impact_bps"]
    large = sqrt_impact_cost_bps(
        trade_value_inr=1e8, avg_daily_value_inr=1e9, daily_vol_pct=2.0)["impact_bps"]
    assert large > small > 0.0

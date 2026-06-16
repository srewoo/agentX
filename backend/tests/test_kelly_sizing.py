from __future__ import annotations
"""Tests for app.services.kelly_sizing — edge-aware Kelly position sizing.

The contract we lock in here:
  • negative-edge trades size to ZERO (the filter),
  • positive-edge trades size proportionally to edge,
  • caps (position %, per-trade risk %) bind correctly,
  • the payoff ratio comes from real trade geometry.
"""
import math

import pytest

from app.services.kelly_sizing import (
    payoff_ratio,
    kelly_fraction,
    kelly_position_size,
    wilson_lower_bound,
    MAX_WIN_PROB,
)


class TestPayoffRatio:
    def test_bullish_reward_over_risk(self):
        # entry 100, stop 95 (risk 5), target 115 (reward 15) -> b = 3.0
        assert payoff_ratio(100, 95, 115, "bullish") == pytest.approx(3.0)

    def test_bearish_is_mirrored(self):
        # short at 100, stop 105 (risk 5), target 90 (reward 10) -> b = 2.0
        assert payoff_ratio(100, 105, 90, "bearish") == pytest.approx(2.0)

    def test_invalid_geometry_returns_none(self):
        assert payoff_ratio(100, 105, 115, "bullish") is None  # stop above entry on a long
        assert payoff_ratio(100, 95, 95, "bullish") is None     # zero reward
        assert payoff_ratio(0, 0, 0, "bullish") is None


class TestKellyFraction:
    def test_known_value(self):
        # p=0.6, b=2 -> f* = 0.6 - 0.4/2 = 0.4
        assert kelly_fraction(0.6, 2.0) == pytest.approx(0.4)

    def test_negative_edge_clamps_to_zero(self):
        # p=0.4, b=1 -> f* = 0.4 - 0.6 = -0.2 -> 0
        assert kelly_fraction(0.4, 1.0) == 0.0

    def test_breakeven_is_zero(self):
        # p=0.5, b=1 -> f* = 0.5 - 0.5 = 0
        assert kelly_fraction(0.5, 1.0) == 0.0

    def test_zero_payoff_is_zero(self):
        assert kelly_fraction(0.9, 0.0) == 0.0


class TestKellyPositionSize:
    def test_positive_edge_sizes_above_zero(self):
        r = kelly_position_size(
            capital=1_000_000, entry=100, stop=95, target=115,
            win_prob=0.6, kelly_fraction_mult=0.25,
            max_position_pct=100, max_risk_pct=100,  # relax caps to isolate Kelly
        )
        # f_full = 0.6 - 0.4/3 = 0.4667; quarter = 0.1167 -> 11.67% of 1M / 100
        assert not r["skip"]
        assert r["kelly_f_full"] == pytest.approx(0.4667, abs=1e-3)
        assert r["kelly_f_used"] == pytest.approx(0.1167, abs=1e-3)
        # Sizing uses the unrounded fraction: f_used = (0.6 - 0.4/3) * 0.25
        f_used = (0.6 - 0.4 / 3) * 0.25
        assert r["shares"] == int(f_used * 1_000_000 / 100)
        assert r["binding_constraint"] == "kelly"

    def test_negative_edge_skips(self):
        # Poor odds + coin-flip prob -> non-positive Kelly -> skip, zero shares.
        r = kelly_position_size(
            capital=1_000_000, entry=100, stop=90, target=105,  # b = 0.5
            win_prob=0.5,
        )
        assert r["skip"] is True
        assert r["shares"] == 0
        assert "non-positive Kelly edge" in r["reason"]

    def test_position_cap_binds(self):
        # Huge Kelly edge but 5% position cap should bind.
        r = kelly_position_size(
            capital=1_000_000, entry=100, stop=99, target=130,
            win_prob=0.9, kelly_fraction_mult=1.0,
            max_position_pct=5.0, max_risk_pct=100,
        )
        assert not r["skip"]
        assert r["binding_constraint"] == "position_cap"
        assert r["capital_pct"] == pytest.approx(5.0, abs=0.5)

    def test_risk_cap_binds(self):
        # Wide stop (risk 10/share) with 1% risk cap caps shares at 1000.
        r = kelly_position_size(
            capital=1_000_000, entry=100, stop=90, target=160,
            win_prob=0.8, kelly_fraction_mult=1.0,
            max_position_pct=100, max_risk_pct=1.0,
        )
        assert not r["skip"]
        assert r["binding_constraint"] == "risk_cap"
        assert r["risk_amount"] == pytest.approx(10_000, abs=10)  # 1% of 1M

    def test_invalid_geometry_skips(self):
        r = kelly_position_size(
            capital=1_000_000, entry=100, stop=105, target=120, win_prob=0.7,
        )
        assert r["skip"] is True
        assert "geometry" in r["reason"]

    def test_zero_capital_skips(self):
        r = kelly_position_size(capital=0, entry=100, stop=95, target=115, win_prob=0.7)
        assert r["skip"] is True

    def test_higher_prob_sizes_larger(self):
        kw = dict(capital=1_000_000, entry=100, stop=95, target=115,
                  kelly_fraction_mult=0.25, max_position_pct=100, max_risk_pct=100)
        lo = kelly_position_size(win_prob=0.55, **kw)
        hi = kelly_position_size(win_prob=0.70, **kw)
        assert hi["shares"] > lo["shares"]


class TestConservativeGuards:
    """The structural anti-oversizing guards — a caller cannot size for ruin."""

    def test_win_prob_ceiling_caps_edge(self):
        # An over-optimistic p=0.99 must be treated as no more than MAX_WIN_PROB.
        f_absurd = kelly_fraction(0.99, 2.0)
        f_capped = kelly_fraction(MAX_WIN_PROB, 2.0)
        assert f_absurd == pytest.approx(f_capped)

    def test_wilson_lower_bound_shrinks_small_samples(self):
        # Same 55% rate: a tiny sample is shrunk far below a large one.
        small = wilson_lower_bound(11, 20)   # 55% on 20 trades
        large = wilson_lower_bound(1100, 2000)  # 55% on 2000 trades
        assert small < large < 0.55
        assert wilson_lower_bound(0, 0) == 0.0

    def test_win_prob_n_uses_wilson_lower_bound(self):
        # A lucky 60% on only 25 trades should be sized off its Wilson LB,
        # producing a strictly smaller (or zero) position than trusting 0.60.
        kw = dict(capital=1_000_000, entry=100, stop=95, target=115,
                  kelly_fraction_mult=0.25, max_position_pct=100, max_risk_pct=100)
        trusting = kelly_position_size(win_prob=0.60, **kw)
        conservative = kelly_position_size(win_prob=0.60, win_prob_n=25, **kw)
        assert conservative["win_prob_used"] < conservative["win_prob_raw"]
        assert conservative["shares"] < trusting["shares"]

    def test_small_sample_can_drop_marginal_trade(self):
        # b=1 (1:1 odds), 55% on 30 trades -> Wilson LB ~0.37 -> negative edge -> skip.
        r = kelly_position_size(
            capital=1_000_000, entry=100, stop=90, target=110,  # b = 1.0
            win_prob=0.55, win_prob_n=30,
        )
        assert r["skip"] is True

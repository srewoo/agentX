from __future__ import annotations
"""Tests for backtester fixes — neutral signal handling and transaction costs."""

import pytest

from app.services.backtester import (
    _evaluate_outcome,
    TRANSACTION_COST_PCT,
    BULLISH,
    BEARISH,
)


class TestEvaluateOutcomeNeutralFix:
    def test_neutral_signal_is_not_win(self):
        result = _evaluate_outcome("neutral", 100.0, 105.0)
        assert result["win"] is False
        assert result["neutral"] is True

    def test_neutral_pnl_is_absolute_move(self):
        result = _evaluate_outcome("neutral", 100.0, 95.0)
        assert result["pnl_pct"] == 5.0  # abs(-5%) = 5%
        assert result["neutral"] is True

    def test_bullish_not_neutral(self):
        result = _evaluate_outcome(BULLISH, 100.0, 105.0)
        assert result["neutral"] is False

    def test_bearish_not_neutral(self):
        result = _evaluate_outcome(BEARISH, 100.0, 95.0)
        assert result["neutral"] is False


class TestTransactionCosts:
    def test_cost_subtracted_from_bullish(self):
        result = _evaluate_outcome(BULLISH, 100.0, 101.0)
        expected = 1.0 - TRANSACTION_COST_PCT  # 1% move - 0.2% cost = 0.8%
        assert abs(result["pnl_pct"] - expected) < 0.001

    def test_cost_subtracted_from_bearish(self):
        result = _evaluate_outcome(BEARISH, 100.0, 99.0)
        expected = 1.0 - TRANSACTION_COST_PCT
        assert abs(result["pnl_pct"] - expected) < 0.001

    def test_marginal_gain_becomes_loss_after_cost(self):
        """A 0.1% gain is a loss after 0.2% transaction costs."""
        result = _evaluate_outcome(BULLISH, 100.0, 100.1)
        assert result["pnl_pct"] < 0
        assert result["win"] is False

    def test_no_cost_on_neutral(self):
        """Neutral signals don't have transaction costs (they're not traded)."""
        result = _evaluate_outcome("neutral", 100.0, 105.0)
        assert result["pnl_pct"] == 5.0  # pure absolute move, no cost

    def test_custom_cost(self):
        result = _evaluate_outcome(BULLISH, 100.0, 102.0, transaction_cost_pct=0.5)
        expected = 2.0 - 0.5
        assert abs(result["pnl_pct"] - expected) < 0.001

    def test_zero_cost_option(self):
        result = _evaluate_outcome(BULLISH, 100.0, 102.0, transaction_cost_pct=0.0)
        assert abs(result["pnl_pct"] - 2.0) < 0.001

    def test_zero_entry_price(self):
        result = _evaluate_outcome(BULLISH, 0.0, 100.0)
        assert result["pnl_pct"] == 0.0
        assert result["win"] is False
        assert result["neutral"] is False


class TestVixThresholds:
    """Test the VIX-adjusted threshold logic from market_data.py."""

    def test_low_vix_tightens_thresholds(self):
        from app.services.market_data import get_vix_adjusted_thresholds
        base = {"price_spike_pct": "3.0", "volume_spike_ratio": "2.0", "rsi_overbought": "70"}
        result = get_vix_adjusted_thresholds(12.0, base)
        assert float(result["price_spike_pct"]) < 3.0
        assert float(result["volume_spike_ratio"]) < 2.0

    def test_normal_vix_unchanged(self):
        from app.services.market_data import get_vix_adjusted_thresholds
        base = {"price_spike_pct": "3.0", "volume_spike_ratio": "2.0"}
        result = get_vix_adjusted_thresholds(16.0, base)
        assert result["price_spike_pct"] == "3.0"

    def test_high_vix_widens_thresholds(self):
        from app.services.market_data import get_vix_adjusted_thresholds
        base = {"price_spike_pct": "3.0", "volume_spike_ratio": "2.0", "rsi_overbought": "70", "rsi_oversold": "30"}
        result = get_vix_adjusted_thresholds(25.0, base)
        assert float(result["price_spike_pct"]) > 3.0
        assert float(result["volume_spike_ratio"]) > 2.0

    def test_crisis_vix_doubles_thresholds(self):
        from app.services.market_data import get_vix_adjusted_thresholds
        base = {"price_spike_pct": "3.0", "volume_spike_ratio": "2.0", "rsi_overbought": "70", "rsi_oversold": "30"}
        result = get_vix_adjusted_thresholds(35.0, base)
        assert float(result["price_spike_pct"]) >= 6.0
        assert float(result["volume_spike_ratio"]) >= 4.0

    def test_none_vix_returns_base(self):
        from app.services.market_data import get_vix_adjusted_thresholds
        base = {"price_spike_pct": "3.0"}
        result = get_vix_adjusted_thresholds(None, base)
        assert result == base

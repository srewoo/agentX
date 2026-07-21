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


class TestFoldMetricsCounts:
    """Item 7: _fold_metrics must expose raw wins/evaluated counts so the
    autonomous gating loop can build OOS significance candidates directly,
    excluding neutral trades. Pins the contract the orchestrator depends on."""

    def test_exposes_wins_and_evaluated_excluding_neutral(self):
        from app.services.backtester_walk_forward import _fold_metrics
        trades = [
            {"win_5d": True, "pnl_5d": 2.0},
            {"win_5d": False, "pnl_5d": -1.0},
            {"win_5d": True, "pnl_5d": 1.5},
            {"neutral_5d": True, "win_5d": False, "pnl_5d": 0.0},  # excluded
        ]
        m = _fold_metrics(trades, [5])
        assert m["wins_5d"] == 2
        assert m["evaluated_5d"] == 3  # neutral excluded
        assert m["win_rate_5d"] == pytest.approx(66.67, abs=0.01)


class TestSimulatePathExit:
    """Item 8: backtest exit model must match live (stop/target/time), not a
    fixed-horizon mark-to-close. These pin the deterministic exit logic."""

    def _f(self):
        from app.services.backtester_walk_forward import _simulate_path_exit
        return _simulate_path_exit

    def test_bullish_stop_hit(self):
        # entry 100, stop 95, target 110. Bar 2 low pierces 95 → stop.
        px, reason, held = self._f()("bullish", 100, 95, 110,
                                     highs=[102, 103], lows=[99, 94], closes=[101, 96])
        assert (px, reason, held) == (95, "stop", 2)

    def test_bullish_target_hit(self):
        px, reason, held = self._f()("bullish", 100, 95, 110,
                                     highs=[111], lows=[99], closes=[110])
        assert (px, reason, held) == (110, "target", 1)

    def test_bullish_time_exit(self):
        # Neither stop nor target touched → exit at last close.
        px, reason, held = self._f()("bullish", 100, 95, 110,
                                     highs=[104, 105], lows=[98, 99], closes=[103, 104])
        assert (px, reason, held) == (104, "time", 2)

    def test_bearish_stop_hit(self):
        # short: stop ABOVE entry (105), target BELOW (90). High pierces 105.
        px, reason, held = self._f()("bearish", 100, 105, 90,
                                     highs=[106], lows=[99], closes=[104])
        assert (px, reason, held) == (105, "stop", 1)

    def test_bearish_target_hit(self):
        px, reason, held = self._f()("bearish", 100, 105, 90,
                                     highs=[101], lows=[89], closes=[91])
        assert (px, reason, held) == (90, "target", 1)

    def test_stop_wins_when_bar_spans_both(self):
        # Bar range engulfs BOTH stop(95) and target(110): risk-first → stop.
        px, reason, held = self._f()("bullish", 100, 95, 110,
                                     highs=[111], lows=[94], closes=[100])
        assert (px, reason, held) == (95, "stop", 1)

    def test_empty_path_returns_entry(self):
        px, reason, held = self._f()("bullish", 100, 95, 110,
                                     highs=[], lows=[], closes=[])
        assert (px, reason, held) == (100, "time", 0)

    # ── 2.5 gap-through slippage on exits ──
    def test_bullish_gap_down_through_stop_fills_at_open(self):
        # Bar 1 opens at 92 — gapped BELOW the 95 stop. Real fill is the open
        # (a bigger loss), not the stop price.
        px, reason, held = self._f()("bullish", 100, 95, 110,
                                     highs=[96], lows=[90], closes=[93], opens=[92])
        assert (px, reason, held) == (92, "stop", 1)

    def test_bearish_gap_up_through_stop_fills_at_open(self):
        # Short with stop 105; bar opens at 108 — gapped ABOVE the stop.
        px, reason, held = self._f()("bearish", 100, 105, 90,
                                     highs=[110], lows=[104], closes=[109], opens=[108])
        assert (px, reason, held) == (108, "stop", 1)

    def test_intrabar_stop_still_fills_at_stop_not_open(self):
        # Open 99 (above stop 95), then trades down to touch 95 intrabar →
        # ordinary fill AT the stop, no gap penalty.
        px, reason, held = self._f()("bullish", 100, 95, 110,
                                     highs=[100], lows=[94], closes=[97], opens=[99])
        assert (px, reason, held) == (95, "stop", 1)

    def test_target_gap_is_not_over_credited(self):
        # Bar gaps ABOVE the target at the open — we do NOT credit the extra
        # move; the target fills at the target price (pessimistic-only model).
        px, reason, held = self._f()("bullish", 100, 95, 110,
                                     highs=[115], lows=[108], closes=[114], opens=[112])
        assert (px, reason, held) == (110, "target", 1)

    def test_gap_disabled_when_opens_omitted(self):
        # Same gap-down bar but no opens supplied → legacy fill-at-stop.
        px, reason, held = self._f()("bullish", 100, 95, 110,
                                     highs=[96], lows=[90], closes=[93])
        assert (px, reason, held) == (95, "stop", 1)

from __future__ import annotations
"""Tests for app.services.risk_manager — ATR position sizing, portfolio heat, trailing stops."""

import pytest

from app.services.risk_manager import (
    calculate_position_size,
    calculate_portfolio_heat,
    update_trailing_stop,
)


# ─────────────────────────────────────────────
# calculate_position_size
# ─────────────────────────────────────────────

class TestCalculatePositionSize:
    def test_basic_bullish_position(self):
        result = calculate_position_size(capital=1_000_000, entry_price=500.0, atr=15.0)
        assert result["shares"] > 0
        assert result["stop_loss"] == round(500.0 - 15.0 * 2.0, 2)  # entry - ATR*2
        assert result["position_value"] == result["shares"] * 500.0

    def test_bearish_stop_is_above_entry(self):
        result = calculate_position_size(capital=1_000_000, entry_price=500.0, atr=15.0, direction="bearish")
        assert result["stop_loss"] > 500.0
        assert result["stop_loss"] == round(500.0 + 15.0 * 2.0, 2)

    def test_zero_entry_price_returns_zero_shares(self):
        result = calculate_position_size(capital=1_000_000, entry_price=0, atr=15.0)
        assert result["shares"] == 0

    def test_zero_atr_returns_zero_shares(self):
        result = calculate_position_size(capital=1_000_000, entry_price=500.0, atr=0)
        assert result["shares"] == 0

    def test_negative_entry_returns_zero_shares(self):
        result = calculate_position_size(capital=1_000_000, entry_price=-100.0, atr=15.0)
        assert result["shares"] == 0

    def test_position_capped_at_max_5_pct(self):
        """With a tiny ATR, shares would be huge — must be capped at 5% of capital."""
        result = calculate_position_size(capital=1_000_000, entry_price=10.0, atr=0.1)
        max_position = 1_000_000 * 0.05
        assert result["position_value"] <= max_position

    def test_custom_risk_pct(self):
        result_1pct = calculate_position_size(capital=1_000_000, entry_price=500.0, atr=15.0, risk_per_trade_pct=1.0)
        result_2pct = calculate_position_size(capital=1_000_000, entry_price=500.0, atr=15.0, risk_per_trade_pct=2.0)
        assert result_2pct["shares"] >= result_1pct["shares"]

    def test_custom_atr_multiplier(self):
        result_2x = calculate_position_size(capital=1_000_000, entry_price=500.0, atr=15.0, atr_multiplier=2.0)
        result_3x = calculate_position_size(capital=1_000_000, entry_price=500.0, atr=15.0, atr_multiplier=3.0)
        # Wider stop → fewer shares
        assert result_3x["shares"] <= result_2x["shares"]
        assert result_3x["stop_distance"] > result_2x["stop_distance"]

    def test_very_expensive_stock_gets_few_shares(self):
        result = calculate_position_size(capital=1_000_000, entry_price=50_000.0, atr=1000.0)
        assert result["shares"] >= 0
        assert result["position_value"] <= 1_000_000 * 0.05

    def test_risk_amount_correct(self):
        result = calculate_position_size(capital=1_000_000, entry_price=500.0, atr=15.0)
        expected_risk = result["shares"] * result["stop_distance"]
        assert abs(result["risk_amount"] - expected_risk) < 1.0


# ─────────────────────────────────────────────
# calculate_portfolio_heat
# ─────────────────────────────────────────────

class TestCalculatePortfolioHeat:
    def test_empty_portfolio_returns_zero(self):
        result = calculate_portfolio_heat([])
        assert result["total_heat"] == 0.0
        assert result["positions"] == []

    def test_single_position(self):
        trades = [{"symbol": "RELIANCE", "entry_price": 2500, "stop_loss": 2425, "shares": 40}]
        result = calculate_portfolio_heat(trades)
        expected = 40 * abs(2500 - 2425)
        assert result["total_heat"] == expected
        assert len(result["positions"]) == 1
        assert result["positions"][0]["symbol"] == "RELIANCE"

    def test_multiple_positions_sum(self):
        trades = [
            {"symbol": "TCS", "entry_price": 3800, "stop_loss": 3700, "shares": 20},
            {"symbol": "INFY", "entry_price": 1500, "stop_loss": 1455, "shares": 50},
        ]
        result = calculate_portfolio_heat(trades)
        expected = 20 * 100 + 50 * 45
        assert result["total_heat"] == expected

    def test_malformed_trade_skipped(self):
        trades = [
            {"symbol": "OK", "entry_price": 100, "stop_loss": 97, "shares": 10},
            {"symbol": "BAD", "entry_price": "invalid", "stop_loss": 0, "shares": "abc"},
        ]
        result = calculate_portfolio_heat(trades)
        assert len(result["positions"]) == 1

    def test_zero_stop_skipped(self):
        trades = [{"symbol": "X", "entry_price": 100, "stop_loss": 0, "shares": 10}]
        result = calculate_portfolio_heat(trades)
        assert result["total_heat"] == 0.0


# ─────────────────────────────────────────────
# update_trailing_stop
# ─────────────────────────────────────────────

class TestUpdateTrailingStop:
    def test_no_move_keeps_original_stop(self):
        stop = update_trailing_stop(entry_price=100, current_stop=97, current_price=100, direction="bullish")
        assert stop == 97

    def test_bullish_1_5pct_move_locks_breakeven(self):
        stop = update_trailing_stop(entry_price=100, current_stop=97, current_price=101.6, direction="bullish")
        assert stop == 100  # breakeven

    def test_bullish_3pct_move_trails(self):
        stop = update_trailing_stop(entry_price=100, current_stop=97, current_price=104.0, direction="bullish")
        expected = round(104.0 * 0.97, 2)
        assert stop == expected

    def test_bullish_never_moves_stop_down(self):
        """If price retraces after trailing up, stop should stay put."""
        stop1 = update_trailing_stop(entry_price=100, current_stop=97, current_price=106.0, direction="bullish")
        stop2 = update_trailing_stop(entry_price=100, current_stop=stop1, current_price=103.5, direction="bullish")
        assert stop2 >= stop1  # never moves stop backward

    def test_bearish_1_5pct_move_locks_breakeven(self):
        stop = update_trailing_stop(entry_price=100, current_stop=103, current_price=98.4, direction="bearish")
        assert stop == 100

    def test_bearish_3pct_move_trails(self):
        stop = update_trailing_stop(entry_price=100, current_stop=103, current_price=96.0, direction="bearish")
        expected = round(96.0 * 1.03, 2)
        assert stop == expected

    def test_bearish_never_moves_stop_up(self):
        stop1 = update_trailing_stop(entry_price=100, current_stop=103, current_price=94.0, direction="bearish")
        stop2 = update_trailing_stop(entry_price=100, current_stop=stop1, current_price=97.0, direction="bearish")
        assert stop2 <= stop1

    def test_zero_entry_returns_current_stop(self):
        stop = update_trailing_stop(entry_price=0, current_stop=97, current_price=105, direction="bullish")
        assert stop == 97

    def test_small_move_below_threshold_no_change(self):
        stop = update_trailing_stop(entry_price=100, current_stop=97, current_price=101.0, direction="bullish")
        assert stop == 97  # 1% move < 1.5% threshold

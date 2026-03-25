from __future__ import annotations

"""Tests for app.services.technicals — compute_technicals, support/resistance, Fibonacci."""

import numpy as np
import pandas as pd
import pytest

from app.services.technicals import (
    compute_fibonacci_levels,
    compute_support_resistance,
    compute_technicals,
)


# ---------------------------------------------------------------------------
# compute_technicals
# ---------------------------------------------------------------------------

class TestComputeTechnicals:

    def test_given_100_row_df_when_computed_then_returns_expected_keys(self, sample_ohlcv_100):
        result = compute_technicals(sample_ohlcv_100)

        assert isinstance(result, dict)
        expected_keys = {
            "rsi", "adx", "macd", "vwap", "stochastic", "obv", "atr",
            "ichimoku", "cci", "williams_r", "mfi",
        }
        for key in expected_keys:
            assert key in result, f"Missing key: {key}"

    def test_given_100_row_df_when_computed_then_rsi_in_valid_range(self, sample_ohlcv_100):
        result = compute_technicals(sample_ohlcv_100)
        rsi = result.get("rsi")
        if rsi is not None:
            assert 0 <= rsi <= 100, f"RSI {rsi} out of range [0, 100]"

    def test_given_100_row_df_when_computed_then_atr_positive(self, sample_ohlcv_100):
        result = compute_technicals(sample_ohlcv_100)
        atr = result.get("atr")
        if atr is not None:
            assert atr > 0, f"ATR should be positive, got {atr}"

    def test_given_100_row_df_when_computed_then_macd_is_dict(self, sample_ohlcv_100):
        result = compute_technicals(sample_ohlcv_100)
        macd = result.get("macd")
        assert isinstance(macd, dict)
        assert "macd_line" in macd
        assert "signal_line" in macd
        assert "histogram" in macd
        assert "signal" in macd

    def test_given_100_row_df_when_computed_then_stochastic_is_dict(self, sample_ohlcv_100):
        result = compute_technicals(sample_ohlcv_100)
        stoch = result.get("stochastic")
        assert isinstance(stoch, dict)
        assert "k" in stoch
        assert "d" in stoch
        assert "signal" in stoch

    def test_given_100_row_df_when_computed_then_ichimoku_is_dict(self, sample_ohlcv_100):
        result = compute_technicals(sample_ohlcv_100)
        ichi = result.get("ichimoku")
        assert isinstance(ichi, dict)
        assert "tenkan" in ichi
        assert "kijun" in ichi

    def test_given_100_row_df_when_computed_then_bollinger_bands_present(self, sample_ohlcv_100):
        result = compute_technicals(sample_ohlcv_100)
        bb = result.get("bollinger_bands")
        assert isinstance(bb, dict)
        assert "upper" in bb
        assert "lower" in bb
        assert "middle" in bb

    def test_given_100_row_df_when_computed_then_moving_averages_present(self, sample_ohlcv_100):
        result = compute_technicals(sample_ohlcv_100)
        ma = result.get("moving_averages")
        assert isinstance(ma, dict)
        assert "sma20" in ma
        assert "ema20" in ma
        # sma50 should be present for 100-row df
        assert "sma50" in ma

    def test_given_100_row_df_when_computed_then_volume_stats_present(self, sample_ohlcv_100):
        result = compute_technicals(sample_ohlcv_100)
        assert "volume_avg_20" in result
        assert "volume_current" in result

    def test_given_100_row_df_when_computed_then_williams_r_in_range(self, sample_ohlcv_100):
        result = compute_technicals(sample_ohlcv_100)
        wr = result.get("williams_r")
        if wr is not None:
            assert -100 <= wr <= 0, f"Williams %R {wr} out of range [-100, 0]"

    def test_given_100_row_df_when_computed_then_mfi_in_range(self, sample_ohlcv_100):
        result = compute_technicals(sample_ohlcv_100)
        mfi = result.get("mfi")
        if mfi is not None:
            assert 0 <= mfi <= 100, f"MFI {mfi} out of range [0, 100]"

    def test_given_100_row_df_when_computed_then_cci_is_numeric(self, sample_ohlcv_100):
        result = compute_technicals(sample_ohlcv_100)
        cci = result.get("cci")
        if cci is not None:
            assert isinstance(cci, float)

    def test_given_short_df_below_20_rows_when_computed_then_returns_empty(self):
        df = pd.DataFrame(
            {
                "Open": [100] * 15,
                "High": [105] * 15,
                "Low": [95] * 15,
                "Close": [102] * 15,
                "Volume": [10000] * 15,
            }
        )
        result = compute_technicals(df)
        assert result == {}

    def test_given_empty_df_when_computed_then_returns_empty(self):
        df = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        result = compute_technicals(df)
        assert result == {}

    def test_given_exactly_20_rows_when_computed_then_returns_results(self):
        rng = np.random.default_rng(123)
        n = 20
        closes = np.cumsum(rng.normal(0, 1, n)) + 1000
        df = pd.DataFrame(
            {
                "Open": closes - rng.uniform(0, 2, n),
                "High": closes + rng.uniform(1, 5, n),
                "Low": closes - rng.uniform(1, 5, n),
                "Close": closes,
                "Volume": rng.uniform(500_000, 2_000_000, n),
            }
        )
        result = compute_technicals(df)
        assert isinstance(result, dict)
        assert len(result) > 0

    def test_given_100_row_df_when_computed_then_obv_trend_present(self, sample_ohlcv_100):
        result = compute_technicals(sample_ohlcv_100)
        assert "obv_trend" in result
        assert result["obv_trend"] in ("rising", "falling", "flat")

    def test_given_100_row_df_when_computed_then_current_price_present(self, sample_ohlcv_100):
        result = compute_technicals(sample_ohlcv_100)
        assert "current_price" in result
        assert result["current_price"] is not None
        assert result["current_price"] > 0


# ---------------------------------------------------------------------------
# compute_support_resistance
# ---------------------------------------------------------------------------

class TestComputeSupportResistance:

    def test_given_valid_df_when_computed_then_returns_pivot_levels(self, sample_ohlcv_100):
        sr = compute_support_resistance(sample_ohlcv_100)
        assert "pivot" in sr
        assert "resistance" in sr
        assert "support" in sr
        assert sr["pivot"] is not None

    def test_given_valid_df_when_computed_then_resistance_has_r1_r2_r3(self, sample_ohlcv_100):
        sr = compute_support_resistance(sample_ohlcv_100)
        res = sr["resistance"]
        assert "r1" in res
        assert "r2" in res
        assert "r3" in res

    def test_given_valid_df_when_computed_then_support_has_s1_s2_s3(self, sample_ohlcv_100):
        sr = compute_support_resistance(sample_ohlcv_100)
        sup = sr["support"]
        assert "s1" in sup
        assert "s2" in sup
        assert "s3" in sup

    def test_given_valid_df_when_computed_then_r1_above_pivot_above_s1(self, sample_ohlcv_100):
        sr = compute_support_resistance(sample_ohlcv_100)
        pivot = sr["pivot"]
        r1 = sr["resistance"]["r1"]
        s1 = sr["support"]["s1"]
        if all(v is not None for v in [pivot, r1, s1]):
            assert r1 >= pivot >= s1

    def test_given_valid_df_when_computed_then_period_highs_lows_present(self, sample_ohlcv_100):
        sr = compute_support_resistance(sample_ohlcv_100)
        assert "period_highs_lows" in sr
        phl = sr["period_highs_lows"]
        assert "high_52w" in phl
        assert "low_52w" in phl

    def test_given_short_df_below_5_rows_when_computed_then_returns_empty(self):
        df = pd.DataFrame(
            {"Close": [100, 101], "High": [102, 103], "Low": [99, 100], "Volume": [1000, 1100]}
        )
        sr = compute_support_resistance(df)
        assert sr == {}

    def test_given_empty_df_when_computed_then_returns_empty(self):
        df = pd.DataFrame(columns=["Close", "High", "Low", "Volume"])
        sr = compute_support_resistance(df)
        assert sr == {}


# ---------------------------------------------------------------------------
# compute_fibonacci_levels
# ---------------------------------------------------------------------------

class TestComputeFibonacciLevels:

    def test_given_valid_df_when_computed_then_returns_levels(self, sample_ohlcv_100):
        fib = compute_fibonacci_levels(sample_ohlcv_100)
        assert "swing_high" in fib
        assert "swing_low" in fib
        assert "levels" in fib

    def test_given_valid_df_when_computed_then_swing_high_gt_swing_low(self, sample_ohlcv_100):
        fib = compute_fibonacci_levels(sample_ohlcv_100)
        assert fib["swing_high"] > fib["swing_low"]

    def test_given_valid_df_when_computed_then_levels_dict_has_expected_keys(self, sample_ohlcv_100):
        fib = compute_fibonacci_levels(sample_ohlcv_100)
        levels = fib["levels"]
        expected = {"level_0", "level_23_6", "level_38_2", "level_50_0", "level_61_8", "level_78_6", "level_100"}
        assert expected == set(levels.keys())

    def test_given_valid_df_when_computed_then_levels_are_descending(self, sample_ohlcv_100):
        fib = compute_fibonacci_levels(sample_ohlcv_100)
        levels = fib["levels"]
        values = [
            levels["level_0"],
            levels["level_23_6"],
            levels["level_38_2"],
            levels["level_50_0"],
            levels["level_61_8"],
            levels["level_78_6"],
            levels["level_100"],
        ]
        assert values == sorted(values, reverse=True)

    def test_given_valid_df_when_computed_then_level_50_is_midpoint(self, sample_ohlcv_100):
        fib = compute_fibonacci_levels(sample_ohlcv_100)
        midpoint = round((fib["swing_high"] + fib["swing_low"]) / 2, 2)
        assert fib["levels"]["level_50_0"] == midpoint

    def test_given_short_df_when_computed_then_returns_empty(self):
        df = pd.DataFrame(
            {"Close": [100], "High": [105], "Low": [95], "Volume": [1000]}
        )
        fib = compute_fibonacci_levels(df)
        assert fib == {}

    def test_given_constant_price_df_when_computed_then_returns_empty(self):
        """If high == low, diff is 0 and Fibonacci levels are meaningless."""
        n = 30
        df = pd.DataFrame(
            {
                "Open": [500.0] * n,
                "High": [500.0] * n,
                "Low": [500.0] * n,
                "Close": [500.0] * n,
                "Volume": [100_000] * n,
            }
        )
        fib = compute_fibonacci_levels(df)
        assert fib == {}

    def test_given_custom_period_when_computed_then_uses_period(self, sample_ohlcv_100):
        fib_short = compute_fibonacci_levels(sample_ohlcv_100, period=20)
        fib_long = compute_fibonacci_levels(sample_ohlcv_100, period=100)
        # Different periods may produce different swing ranges
        assert isinstance(fib_short, dict)
        assert isinstance(fib_long, dict)
        assert fib_short["swing_high"] is not None
        assert fib_long["swing_high"] is not None

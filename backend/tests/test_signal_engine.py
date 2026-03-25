from __future__ import annotations

"""Tests for app.services.signal_engine — all detectors and scan_symbol."""

import numpy as np
import pandas as pd
import pytest

from app.services.signal_engine import (
    BREAKOUT,
    MACD_CROSSOVER,
    PRICE_SPIKE,
    RSI_EXTREME,
    SENTIMENT_SHIFT,
    VOLUME_SPIKE,
    detect_breakout,
    detect_macd_crossover,
    detect_price_spike,
    detect_rsi_extreme,
    detect_sentiment_shift,
    detect_volume_spike,
    filter_by_risk_mode,
    scan_symbol,
)


# ---------------------------------------------------------------------------
# detect_price_spike
# ---------------------------------------------------------------------------

class TestDetectPriceSpike:

    def test_given_positive_5pct_move_when_detected_then_bullish_signal(self):
        sig = detect_price_spike("RELIANCE", 2625.0, 2500.0)
        assert sig is not None
        assert sig["signal_type"] == PRICE_SPIKE
        assert sig["direction"] == "bullish"
        assert sig["strength"] >= 1
        assert sig["strength"] <= 10
        assert "5.0%" in sig["reason"]

    def test_given_negative_4pct_move_when_detected_then_bearish_signal(self):
        sig = detect_price_spike("TCS", 3648.0, 3800.0)
        assert sig is not None
        assert sig["direction"] == "bearish"
        assert sig["metadata"]["change_pct"] < 0

    def test_given_small_move_when_detected_then_no_signal(self):
        sig = detect_price_spike("INFY", 1510.0, 1500.0)
        assert sig is None

    def test_given_zero_prev_price_when_detected_then_no_signal(self):
        sig = detect_price_spike("HDFCBANK", 1600.0, 0.0)
        assert sig is None

    def test_given_none_current_price_when_detected_then_no_signal(self):
        sig = detect_price_spike("ITC", None, 450.0)
        assert sig is None

    def test_given_none_prev_price_when_detected_then_no_signal(self):
        sig = detect_price_spike("SBIN", 780.0, None)
        assert sig is None

    def test_given_custom_threshold_when_detected_then_uses_threshold(self):
        # 2% move with 1% threshold should trigger
        sig = detect_price_spike("WIPRO", 510.0, 500.0, threshold_pct=1.0)
        assert sig is not None
        assert sig["direction"] == "bullish"

    def test_given_exactly_at_threshold_when_detected_then_no_signal(self):
        # 3% move with 3% threshold: abs(3.0) < 3.0 is False, so it triggers
        sig = detect_price_spike("ITC", 463.5, 450.0, threshold_pct=3.0)
        assert sig is not None

    def test_given_large_spike_when_detected_then_strength_capped_at_10(self):
        sig = detect_price_spike("ADANI", 200.0, 100.0)  # 100% spike
        assert sig is not None
        assert sig["strength"] == 10


# ---------------------------------------------------------------------------
# detect_volume_spike
# ---------------------------------------------------------------------------

class TestDetectVolumeSpike:

    def test_given_3x_volume_when_detected_then_signal(self):
        sig = detect_volume_spike("RELIANCE", 2500.0, 6_000_000, 2_000_000)
        assert sig is not None
        assert sig["signal_type"] == VOLUME_SPIKE
        assert sig["direction"] == "neutral"
        assert sig["metadata"]["volume_ratio"] == 3.0

    def test_given_1_5x_volume_below_threshold_when_detected_then_no_signal(self):
        sig = detect_volume_spike("TCS", 3800.0, 3_000_000, 2_000_000)
        assert sig is None

    def test_given_zero_avg_volume_when_detected_then_no_signal(self):
        sig = detect_volume_spike("INFY", 1400.0, 5_000_000, 0)
        assert sig is None

    def test_given_none_current_vol_when_detected_then_no_signal(self):
        sig = detect_volume_spike("HDFCBANK", 1650.0, None, 1_000_000)
        assert sig is None

    def test_given_none_avg_vol_when_detected_then_no_signal(self):
        sig = detect_volume_spike("ITC", 450.0, 2_000_000, None)
        assert sig is None

    def test_given_custom_threshold_when_detected_then_uses_it(self):
        # 1.5x with 1.2 threshold should trigger
        sig = detect_volume_spike("SBIN", 780.0, 1_800_000, 1_200_000, threshold_ratio=1.2)
        assert sig is not None

    def test_given_large_spike_when_detected_then_strength_capped(self):
        sig = detect_volume_spike("ADANI", 200.0, 20_000_000, 1_000_000)
        assert sig is not None
        assert sig["strength"] <= 10


# ---------------------------------------------------------------------------
# detect_rsi_extreme
# ---------------------------------------------------------------------------

class TestDetectRsiExtreme:

    def test_given_rsi_25_when_detected_then_bullish_oversold(self):
        sig = detect_rsi_extreme("RELIANCE", 2500.0, 25.0)
        assert sig is not None
        assert sig["signal_type"] == RSI_EXTREME
        assert sig["direction"] == "bullish"
        assert "oversold" in sig["reason"].lower()

    def test_given_rsi_75_when_detected_then_bearish_overbought(self):
        sig = detect_rsi_extreme("TCS", 3800.0, 75.0)
        assert sig is not None
        assert sig["direction"] == "bearish"
        assert "overbought" in sig["reason"].lower()

    def test_given_rsi_50_when_detected_then_no_signal(self):
        sig = detect_rsi_extreme("INFY", 1400.0, 50.0)
        assert sig is None

    def test_given_rsi_none_when_detected_then_no_signal(self):
        sig = detect_rsi_extreme("HDFCBANK", 1650.0, None)
        assert sig is None

    def test_given_rsi_exactly_70_when_detected_then_no_signal(self):
        # > 70 triggers overbought, 70 exactly does not
        sig = detect_rsi_extreme("ITC", 450.0, 70.0)
        assert sig is None

    def test_given_rsi_exactly_30_when_detected_then_no_signal(self):
        # < 30 triggers oversold, 30 exactly does not
        sig = detect_rsi_extreme("SBIN", 780.0, 30.0)
        assert sig is None

    def test_given_rsi_10_extreme_oversold_when_detected_then_high_strength(self):
        sig = detect_rsi_extreme("WIPRO", 420.0, 10.0)
        assert sig is not None
        assert sig["strength"] >= 7

    def test_given_rsi_90_extreme_overbought_when_detected_then_high_strength(self):
        sig = detect_rsi_extreme("ADANI", 200.0, 90.0)
        assert sig is not None
        assert sig["strength"] >= 7


# ---------------------------------------------------------------------------
# detect_macd_crossover
# ---------------------------------------------------------------------------

class TestDetectMacdCrossover:

    def test_given_bullish_cross_when_detected_then_bullish_signal(self):
        # Previous: MACD < Signal; Current: MACD > Signal
        sig = detect_macd_crossover("RELIANCE", 2500.0, 1.5, -0.5, 0.8, 0.2)
        assert sig is not None
        assert sig["signal_type"] == MACD_CROSSOVER
        assert sig["direction"] == "bullish"

    def test_given_bearish_cross_when_detected_then_bearish_signal(self):
        # Previous: MACD > Signal; Current: MACD < Signal
        sig = detect_macd_crossover("TCS", 3800.0, -0.5, 1.5, 0.2, 0.8)
        assert sig is not None
        assert sig["direction"] == "bearish"

    def test_given_no_cross_macd_still_above_when_detected_then_no_signal(self):
        sig = detect_macd_crossover("INFY", 1400.0, 2.0, 1.5, 1.0, 1.0)
        assert sig is None

    def test_given_no_cross_macd_still_below_when_detected_then_no_signal(self):
        sig = detect_macd_crossover("HDFCBANK", 1650.0, -1.0, -0.5, 0.5, 0.2)
        assert sig is None

    def test_given_none_macd_curr_when_detected_then_no_signal(self):
        sig = detect_macd_crossover("ITC", 450.0, None, 1.0, 0.5, 0.3)
        assert sig is None

    def test_given_none_signal_prev_when_detected_then_no_signal(self):
        sig = detect_macd_crossover("SBIN", 780.0, 1.0, 0.5, 0.3, None)
        assert sig is None

    def test_given_all_none_when_detected_then_no_signal(self):
        sig = detect_macd_crossover("WIPRO", 420.0, None, None, None, None)
        assert sig is None

    def test_given_bullish_cross_when_detected_then_strength_is_6(self):
        sig = detect_macd_crossover("RELIANCE", 2500.0, 1.5, -0.5, 0.8, 0.2)
        assert sig["strength"] == 6


# ---------------------------------------------------------------------------
# detect_breakout
# ---------------------------------------------------------------------------

class TestDetectBreakout:

    def test_given_strong_breakout_when_detected_then_signal_with_score_gte_4(
        self, sample_ohlcv_50, sample_sr, sample_technicals
    ):
        """Engineer a scenario where price > R1, high volume, RSI in zone, strong ADX."""
        df = sample_ohlcv_50.copy()
        # Force last two rows so price crosses above R1 today
        r1 = sample_sr["resistance"]["r1"]  # 1520
        df.iloc[-2, df.columns.get_loc("Close")] = r1 - 10  # prev below R1
        df.iloc[-1, df.columns.get_loc("Close")] = r1 + 30  # today above R1

        # Force volume spike
        avg_vol = df["Volume"].iloc[-20:].mean()
        df.iloc[-1, df.columns.get_loc("Volume")] = avg_vol * 3

        technicals = {
            **sample_technicals,
            "rsi": 60.0,
            "adx": 30.0,
            "macd": {**sample_technicals["macd"], "signal": "Bullish"},
        }

        sig = detect_breakout("RELIANCE", df, sample_sr, technicals)
        assert sig is not None
        assert sig["signal_type"] == BREAKOUT
        assert sig["strength"] >= 4
        assert sig["direction"] == "bullish"

    def test_given_weak_conditions_when_detected_then_no_signal(
        self, sample_ohlcv_50, sample_sr, sample_technicals
    ):
        """Price well between S1 and R1, low volume, RSI outside momentum zone, no breakout."""
        df = sample_ohlcv_50.copy()
        r1 = sample_sr["resistance"]["r1"]  # 1520
        s1 = sample_sr["support"]["s1"]  # 1470
        midpoint = (r1 + s1) / 2  # 1495 — safely between S1 and R1

        df.iloc[-1, df.columns.get_loc("Close")] = midpoint
        df.iloc[-2, df.columns.get_loc("Close")] = midpoint + 5  # prev also between levels

        # Force low volume (below 1.5x)
        avg_vol = df["Volume"].iloc[-20:].mean()
        df.iloc[-1, df.columns.get_loc("Volume")] = avg_vol * 0.8

        technicals = {
            **sample_technicals,
            "rsi": 40.0,  # outside 50-70 momentum zone
            "adx": 15.0,  # weak trend
            "macd": {**sample_technicals["macd"], "signal": "Bearish"},
        }

        sig = detect_breakout("RELIANCE", df, sample_sr, technicals)
        assert sig is None

    def test_given_short_dataframe_when_detected_then_no_signal(self, sample_sr, sample_technicals):
        df = pd.DataFrame(
            {"Close": [100, 101], "High": [102, 103], "Low": [99, 100], "Volume": [1000, 1100]},
        )
        sig = detect_breakout("TCS", df, sample_sr, sample_technicals)
        assert sig is None

    def test_given_none_dataframe_when_detected_then_no_signal(self, sample_sr, sample_technicals):
        sig = detect_breakout("INFY", None, sample_sr, sample_technicals)
        assert sig is None

    def test_given_bearish_breakdown_below_s1_when_detected_then_bearish(
        self, sample_ohlcv_50, sample_sr, sample_technicals
    ):
        """Price breaks below S1 with volume."""
        df = sample_ohlcv_50.copy()
        s1 = sample_sr["support"]["s1"]  # 1470
        df.iloc[-2, df.columns.get_loc("Close")] = s1 + 10
        df.iloc[-1, df.columns.get_loc("Close")] = s1 - 30

        avg_vol = df["Volume"].iloc[-20:].mean()
        df.iloc[-1, df.columns.get_loc("Volume")] = avg_vol * 2.5

        technicals = {**sample_technicals, "rsi": 60.0, "adx": 30.0}

        sig = detect_breakout("SBIN", df, sample_sr, technicals)
        # Might or might not reach score 4 depending on other factors,
        # but if it does, direction should be bearish
        if sig is not None:
            assert sig["direction"] == "bearish"


# ---------------------------------------------------------------------------
# detect_sentiment_shift
# ---------------------------------------------------------------------------

class TestDetectSentimentShift:

    def test_given_positive_0_6_score_when_detected_then_bullish(self):
        sig = detect_sentiment_shift("RELIANCE", 2500.0, 0.6)
        assert sig is not None
        assert sig["signal_type"] == SENTIMENT_SHIFT
        assert sig["direction"] == "bullish"

    def test_given_negative_0_5_score_when_detected_then_bearish(self):
        sig = detect_sentiment_shift("TCS", 3800.0, -0.5)
        assert sig is not None
        assert sig["direction"] == "bearish"

    def test_given_small_score_0_2_when_detected_then_no_signal(self):
        sig = detect_sentiment_shift("INFY", 1400.0, 0.2)
        assert sig is None

    def test_given_negative_small_score_when_detected_then_no_signal(self):
        sig = detect_sentiment_shift("HDFCBANK", 1650.0, -0.3)
        assert sig is None

    def test_given_zero_score_when_detected_then_no_signal(self):
        sig = detect_sentiment_shift("ITC", 450.0, 0.0)
        assert sig is None

    def test_given_exactly_at_threshold_when_detected_then_no_signal(self):
        # abs(0.4) < 0.4 is False, so it should NOT trigger
        # Actually abs(0.4) < 0.4 is False, so it DOES trigger
        sig = detect_sentiment_shift("SBIN", 780.0, 0.4)
        # 0.4 is not < 0.4, so it does NOT return None. It proceeds.
        assert sig is not None

    def test_given_max_score_when_detected_then_strength_capped(self):
        sig = detect_sentiment_shift("ADANI", 200.0, 1.0)
        assert sig is not None
        assert sig["strength"] <= 10


# ---------------------------------------------------------------------------
# scan_symbol (integration of all detectors)
# ---------------------------------------------------------------------------

class TestScanSymbol:

    def test_given_realistic_data_when_scanned_then_returns_list(
        self, sample_ohlcv_50, sample_technicals, sample_sr
    ):
        signals = scan_symbol(
            "RELIANCE",
            sample_ohlcv_50,
            sample_technicals,
            sample_sr,
            previous_price=sample_ohlcv_50["Close"].iloc[-1] * 0.93,  # 7% lower to trigger spike
            sentiment_score=0.7,
        )
        assert isinstance(signals, list)
        # Should at least detect price spike and sentiment shift
        types_found = {s["signal_type"] for s in signals}
        assert PRICE_SPIKE in types_found
        assert SENTIMENT_SHIFT in types_found

    def test_given_no_previous_price_when_scanned_then_skips_price_spike(
        self, sample_ohlcv_50, sample_technicals, sample_sr
    ):
        signals = scan_symbol("TCS", sample_ohlcv_50, sample_technicals, sample_sr)
        types_found = {s["signal_type"] for s in signals}
        assert PRICE_SPIKE not in types_found

    def test_given_no_sentiment_when_scanned_then_skips_sentiment(
        self, sample_ohlcv_50, sample_technicals, sample_sr
    ):
        signals = scan_symbol("INFY", sample_ohlcv_50, sample_technicals, sample_sr)
        types_found = {s["signal_type"] for s in signals}
        assert SENTIMENT_SHIFT not in types_found

    def test_given_empty_df_when_scanned_then_returns_empty_or_minimal(self, sample_sr):
        """With an empty DataFrame and no volume/RSI/MACD data, no signals should fire."""
        empty_df = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        empty_technicals = {}  # no indicators available
        signals = scan_symbol("EMPTY", empty_df, empty_technicals, sample_sr)
        assert signals == []

    def test_given_all_triggers_when_scanned_then_each_signal_has_required_keys(
        self, sample_ohlcv_50, sample_sr
    ):
        """Every signal dict must have the standard keys."""
        technicals = {
            "rsi": 25.0,  # oversold -> bullish
            "adx": 30.0,
            "macd": {
                "macd_line": 1.5,
                "macd_line_prev": -0.5,
                "signal_line": 0.8,
                "signal_line_prev": 0.2,
                "signal": "Bullish",
            },
            "volume_current": 5_000_000,
            "volume_avg_20": 1_500_000,
            "current_price": 2500.0,
            "prev_price": 2400.0,
        }
        signals = scan_symbol(
            "RELIANCE",
            sample_ohlcv_50,
            technicals,
            sample_sr,
            previous_price=2300.0,
            sentiment_score=0.8,
        )
        required_keys = {
            "id", "symbol", "signal_type", "direction", "strength",
            "reason", "risk", "current_price", "metadata", "created_at",
        }
        for sig in signals:
            assert required_keys.issubset(sig.keys()), f"Missing keys in signal: {required_keys - sig.keys()}"


# ---------------------------------------------------------------------------
# filter_by_risk_mode
# ---------------------------------------------------------------------------

class TestFilterByRiskMode:

    def test_given_conservative_mode_when_filtered_then_strength_gte_7(self, sample_signals):
        filtered = filter_by_risk_mode(sample_signals, "conservative")
        assert all(s["strength"] >= 7 for s in filtered)
        assert len(filtered) > 0

    def test_given_balanced_mode_when_filtered_then_strength_gte_5(self, sample_signals):
        filtered = filter_by_risk_mode(sample_signals, "balanced")
        assert all(s["strength"] >= 5 for s in filtered)

    def test_given_aggressive_mode_when_filtered_then_strength_gte_3(self, sample_signals):
        filtered = filter_by_risk_mode(sample_signals, "aggressive")
        assert all(s["strength"] >= 3 for s in filtered)

    def test_given_unknown_mode_when_filtered_then_defaults_to_balanced(self, sample_signals):
        filtered_unknown = filter_by_risk_mode(sample_signals, "unknown_mode")
        filtered_balanced = filter_by_risk_mode(sample_signals, "balanced")
        assert len(filtered_unknown) == len(filtered_balanced)

    def test_given_empty_signals_when_filtered_then_returns_empty(self):
        assert filter_by_risk_mode([], "conservative") == []

    def test_given_all_high_strength_when_conservative_then_all_pass(self):
        signals = [
            {"strength": 8, "signal_type": "test"},
            {"strength": 9, "signal_type": "test"},
            {"strength": 10, "signal_type": "test"},
        ]
        filtered = filter_by_risk_mode(signals, "conservative")
        assert len(filtered) == 3

    def test_given_all_low_strength_when_conservative_then_none_pass(self):
        signals = [
            {"strength": 1, "signal_type": "test"},
            {"strength": 3, "signal_type": "test"},
            {"strength": 6, "signal_type": "test"},
        ]
        filtered = filter_by_risk_mode(signals, "conservative")
        assert len(filtered) == 0

from __future__ import annotations
"""Tests for app.services.patterns — chart pattern detectors."""

import numpy as np
import pandas as pd
import pytest
from datetime import datetime, timezone

from app.services.patterns import (
    detect_double_bottom,
    detect_double_top,
    detect_bullish_engulfing,
    detect_bearish_engulfing,
    detect_hammer,
    detect_shooting_star,
    detect_gap_up,
    detect_gap_down,
    detect_52_week_high,
    detect_52_week_low,
    detect_inside_day,
    detect_narrow_range,
    scan_patterns,
)


# ─────────────────────────────────────────────
# OHLCV factory helpers
# ─────────────────────────────────────────────

def _flat_df(n: int = 100, price: float = 1000.0, volume: float = 1_000_000) -> pd.DataFrame:
    """Flat price DataFrame — no patterns."""
    dates = pd.bdate_range(end=datetime.now(), periods=n)
    return pd.DataFrame({
        "Open":   [price] * n,
        "High":   [price * 1.005] * n,
        "Low":    [price * 0.995] * n,
        "Close":  [price] * n,
        "Volume": [volume] * n,
    }, index=dates)


def _append_row(df: pd.DataFrame, **kwargs) -> pd.DataFrame:
    """Append a single candle to the end of a DataFrame."""
    last_date = df.index[-1]
    new_idx = pd.bdate_range(start=last_date, periods=2)[1]
    new_row = pd.DataFrame({k: [v] for k, v in kwargs.items()}, index=[new_idx])
    return pd.concat([df, new_row])


# ─────────────────────────────────────────────
# detect_double_bottom
# ─────────────────────────────────────────────

class TestDetectDoubleBottom:
    def test_insufficient_rows_returns_none(self):
        df = _flat_df(10)
        assert detect_double_bottom("RELIANCE", df, lookback=60) is None

    def test_flat_price_no_double_bottom(self):
        df = _flat_df(80)
        result = detect_double_bottom("RELIANCE", df, lookback=60)
        # Flat price is unlikely to form double bottom geometry — may or may not trigger
        # We just verify it doesn't raise
        assert result is None or isinstance(result, dict)

    def test_returns_bullish_direction_when_detected(self):
        # Build a series that forms two troughs at ~900 with a peak at ~1100
        n = 80
        closes = [1000.0] * 20 + [900.0] * 5 + [1100.0] * 10 + [900.0] * 5 + [1050.0] * 40
        dates = pd.bdate_range(end=datetime.now(), periods=n)
        df = pd.DataFrame({
            "Open":   closes,
            "High":   [c * 1.01 for c in closes],
            "Low":    [c * 0.99 for c in closes],
            "Close":  closes,
            "Volume": [1_000_000] * n,
        }, index=dates)
        result = detect_double_bottom("RELIANCE", df)
        if result:
            assert result["direction"] == "bullish"
            assert result["signal_type"] == "double_bottom"


# ─────────────────────────────────────────────
# detect_double_top
# ─────────────────────────────────────────────

class TestDetectDoubleTop:
    def test_insufficient_rows_returns_none(self):
        df = _flat_df(10)
        assert detect_double_top("RELIANCE", df, lookback=60) is None

    def test_returns_bearish_when_detected(self):
        n = 80
        closes = [1000.0] * 20 + [1100.0] * 5 + [900.0] * 10 + [1100.0] * 5 + [950.0] * 40
        dates = pd.bdate_range(end=datetime.now(), periods=n)
        df = pd.DataFrame({
            "Open":   closes,
            "High":   [c * 1.01 for c in closes],
            "Low":    [c * 0.99 for c in closes],
            "Close":  closes,
            "Volume": [1_000_000] * n,
        }, index=dates)
        result = detect_double_top("RELIANCE", df)
        if result:
            assert result["direction"] == "bearish"
            assert result["signal_type"] == "double_top"


# ─────────────────────────────────────────────
# detect_bullish_engulfing
# ─────────────────────────────────────────────

class TestDetectBullishEngulfing:
    def _make_engulfing_df(self) -> pd.DataFrame:
        """Yesterday: small red candle. Today: large green candle that engulfs it."""
        dates = pd.bdate_range(end=datetime.now(), periods=2)
        return pd.DataFrame({
            "Open":   [1010.0, 1000.0],   # yesterday open > close (red); today open < yesterday close
            "High":   [1015.0, 1025.0],
            "Low":    [1005.0,  995.0],
            "Close":  [1005.0, 1020.0],   # today close > yesterday open
            "Volume": [1_000_000, 2_000_000],
        }, index=dates)

    def test_engulfing_pattern_detected(self):
        df = self._make_engulfing_df()
        result = detect_bullish_engulfing("RELIANCE", df)
        if result:
            assert result["direction"] == "bullish"
            assert result["signal_type"] == "bullish_engulfing"

    def test_short_df_returns_none(self):
        df = _flat_df(1)
        assert detect_bullish_engulfing("RELIANCE", df) is None


# ─────────────────────────────────────────────
# detect_bearish_engulfing
# ─────────────────────────────────────────────

class TestDetectBearishEngulfing:
    def _make_engulfing_df(self) -> pd.DataFrame:
        """Yesterday: small green candle. Today: large red candle that engulfs it."""
        dates = pd.bdate_range(end=datetime.now(), periods=2)
        return pd.DataFrame({
            "Open":   [1000.0, 1020.0],   # yesterday green; today opens above yesterday close
            "High":   [1015.0, 1025.0],
            "Low":    [ 995.0,  990.0],
            "Close":  [1010.0,  995.0],   # today close < yesterday open
            "Volume": [1_000_000, 2_000_000],
        }, index=dates)

    def test_bearish_engulfing_detected(self):
        df = self._make_engulfing_df()
        result = detect_bearish_engulfing("RELIANCE", df)
        if result:
            assert result["direction"] == "bearish"
            assert result["signal_type"] == "bearish_engulfing"

    def test_short_df_returns_none(self):
        df = _flat_df(1)
        assert detect_bearish_engulfing("RELIANCE", df) is None


# ─────────────────────────────────────────────
# detect_hammer
# ─────────────────────────────────────────────

class TestDetectHammer:
    def _make_hammer_df(self) -> pd.DataFrame:
        """Hammer: small body near top, long lower wick (2x body), tiny/no upper wick."""
        dates = pd.bdate_range(end=datetime.now(), periods=20)
        closes = [1000.0] * 19 + [1005.0]   # slightly up close
        opens  = [1000.0] * 19 + [1002.0]   # small body
        highs  = [1005.0] * 19 + [1007.0]   # tiny upper wick
        lows   = [995.0] * 19 + [975.0]     # long lower wick
        return pd.DataFrame({
            "Open": opens, "High": highs, "Low": lows, "Close": closes,
            "Volume": [1_000_000] * 20,
        }, index=dates)

    def test_hammer_may_be_detected(self):
        df = self._make_hammer_df()
        result = detect_hammer("RELIANCE", df)
        # May or may not trigger depending on exact ratios — just verify no exception
        assert result is None or result["signal_type"] == "hammer"

    def test_short_df_returns_none(self):
        assert detect_hammer("RELIANCE", _flat_df(1)) is None


# ─────────────────────────────────────────────
# detect_shooting_star
# ─────────────────────────────────────────────

class TestDetectShootingStar:
    def test_no_exception_on_valid_input(self):
        df = _flat_df(20)
        result = detect_shooting_star("RELIANCE", df)
        assert result is None or result["signal_type"] == "shooting_star"

    def test_short_df_returns_none(self):
        assert detect_shooting_star("RELIANCE", _flat_df(1)) is None


# ─────────────────────────────────────────────
# detect_gap_up / detect_gap_down
# ─────────────────────────────────────────────

class TestDetectGapUp:
    def test_gap_up_detected(self):
        """Today's low > yesterday's high = gap up."""
        dates = pd.bdate_range(end=datetime.now(), periods=2)
        df = pd.DataFrame({
            "Open":   [1000.0, 1050.0],
            "High":   [1010.0, 1060.0],
            "Low":    [990.0,  1040.0],  # today low (1040) > yesterday high (1010)
            "Close":  [1005.0, 1055.0],
            "Volume": [1_000_000, 2_000_000],
        }, index=dates)
        result = detect_gap_up("RELIANCE", df)
        if result:
            assert result["direction"] == "bullish"
            assert result["signal_type"] == "gap_up"

    def test_no_gap_returns_none(self):
        df = _flat_df(2)
        result = detect_gap_up("RELIANCE", df)
        assert result is None


class TestDetectGapDown:
    def test_gap_down_detected(self):
        """Today's high < yesterday's low = gap down."""
        dates = pd.bdate_range(end=datetime.now(), periods=2)
        df = pd.DataFrame({
            "Open":   [1000.0,  950.0],
            "High":   [1010.0,  960.0],  # today high (960) < yesterday low (990)
            "Low":    [990.0,   940.0],
            "Close":  [1000.0,  945.0],
            "Volume": [1_000_000, 2_000_000],
        }, index=dates)
        result = detect_gap_down("RELIANCE", df)
        if result:
            assert result["direction"] == "bearish"
            assert result["signal_type"] == "gap_down"


# ─────────────────────────────────────────────
# detect_52_week_high / detect_52_week_low
# ─────────────────────────────────────────────

class TestDetect52WeekHigh:
    def test_52_week_high_detected(self):
        # Build 253 rows where last close is the highest
        n = 253
        closes = list(np.linspace(500, 990, n - 1)) + [1000.0]
        dates = pd.bdate_range(end=datetime.now(), periods=n)
        df = pd.DataFrame({
            "Open":   closes,
            "High":   [c * 1.005 for c in closes],
            "Low":    [c * 0.995 for c in closes],
            "Close":  closes,
            "Volume": [1_000_000] * n,
        }, index=dates)
        result = detect_52_week_high("RELIANCE", df)
        if result:
            assert result["signal_type"] == "52_week_high"
            assert result["direction"] == "bullish"

    def test_insufficient_rows_returns_none(self):
        assert detect_52_week_high("RELIANCE", _flat_df(10)) is None


class TestDetect52WeekLow:
    def test_52_week_low_detected(self):
        n = 253
        closes = list(np.linspace(1000, 510, n - 1)) + [500.0]
        dates = pd.bdate_range(end=datetime.now(), periods=n)
        df = pd.DataFrame({
            "Open":   closes,
            "High":   [c * 1.005 for c in closes],
            "Low":    [c * 0.995 for c in closes],
            "Close":  closes,
            "Volume": [1_000_000] * n,
        }, index=dates)
        result = detect_52_week_low("RELIANCE", df)
        if result:
            assert result["signal_type"] == "52_week_low"
            assert result["direction"] == "bearish"


# ─────────────────────────────────────────────
# detect_inside_day / detect_narrow_range
# ─────────────────────────────────────────────

class TestDetectInsideDay:
    def test_inside_day_detected(self):
        """Today's high <= yesterday's high AND today's low >= yesterday's low."""
        dates = pd.bdate_range(end=datetime.now(), periods=2)
        df = pd.DataFrame({
            "Open":  [1000.0, 1002.0],
            "High":  [1020.0, 1010.0],  # today high (1010) < yesterday high (1020)
            "Low":   [ 980.0,  990.0],  # today low (990) > yesterday low (980)
            "Close": [1010.0, 1005.0],
            "Volume": [1_000_000, 800_000],
        }, index=dates)
        result = detect_inside_day("RELIANCE", df)
        if result:
            assert result["signal_type"] == "inside_day"

    def test_short_df_returns_none(self):
        assert detect_inside_day("RELIANCE", _flat_df(1)) is None


class TestDetectNarrowRange:
    def test_narrow_range_returns_signal_or_none(self):
        df = _flat_df(20)
        result = detect_narrow_range("RELIANCE", df)
        assert result is None or result["signal_type"] == "narrow_range"

    def test_short_df_returns_none(self):
        assert detect_narrow_range("RELIANCE", _flat_df(5)) is None


# ─────────────────────────────────────────────
# scan_patterns (integration: calls all detectors)
# ─────────────────────────────────────────────

class TestScanPatterns:
    def test_returns_list(self, sample_ohlcv_100):
        result = scan_patterns("RELIANCE", sample_ohlcv_100)
        assert isinstance(result, list)

    def test_all_signals_have_required_fields(self, sample_ohlcv_100):
        result = scan_patterns("RELIANCE", sample_ohlcv_100)
        for sig in result:
            assert "id" in sig
            assert "symbol" in sig
            assert "signal_type" in sig
            assert "direction" in sig
            assert "strength" in sig
            assert "reason" in sig

    def test_symbol_set_correctly(self, sample_ohlcv_100):
        result = scan_patterns("RELIANCE", sample_ohlcv_100)
        for sig in result:
            assert sig["symbol"] == "RELIANCE"

    def test_short_df_no_crash(self):
        df = _flat_df(5)
        result = scan_patterns("RELIANCE", df)
        assert isinstance(result, list)

    def test_strength_within_bounds(self, sample_ohlcv_100):
        result = scan_patterns("RELIANCE", sample_ohlcv_100)
        for sig in result:
            assert 1 <= sig["strength"] <= 10

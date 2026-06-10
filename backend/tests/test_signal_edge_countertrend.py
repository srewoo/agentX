from __future__ import annotations

"""Tests for the structural anti-counter-trend guard in app.services.signal_edge.

Regression coverage for the 2026-06-08 fix: the engine was boosting bearish
counter-trend signals inside uptrends (the documented "structurally bearish in
a rising market" failure mode). These tests pin the guard so the regression
cannot silently return.
"""

import pytest

from app.services.signal_edge import (
    COUNTERTREND_ALLOWLIST,
    REGIME_PROMOTE_SET,
    is_countertrend_suppressed,
    signal_weight_multiplier,
)


class TestIsCountertrendSuppressed:
    def test_should_suppress_bearish_signal_in_trend_up_when_not_allowlisted(self):
        assert is_countertrend_suppressed("trend_up", "evening_star", "bearish") is True
        assert is_countertrend_suppressed("trend_up", "rsi_extreme", "bearish") is True

    def test_should_allow_bearish_signal_in_trend_up_when_allowlisted(self):
        assert is_countertrend_suppressed("trend_up", "double_top", "bearish") is False

    def test_should_suppress_bullish_signal_in_trend_down_when_not_allowlisted(self):
        assert is_countertrend_suppressed("trend_down", "hammer", "bullish") is True

    def test_should_not_suppress_with_trend_signals(self):
        # bullish in uptrend / bearish in downtrend are *with* the trend
        assert is_countertrend_suppressed("trend_up", "gap_up", "bullish") is False
        assert is_countertrend_suppressed("trend_down", "gap_down", "bearish") is False

    def test_should_not_suppress_in_sideways_or_unknown_regime(self):
        assert is_countertrend_suppressed("sideways", "rsi_extreme", "bearish") is False
        assert is_countertrend_suppressed(None, "rsi_extreme", "bearish") is False


class TestSignalWeightMultiplierGuard:
    def test_should_zero_weight_bearish_countertrend_in_uptrend(self):
        assert signal_weight_multiplier("evening_star", "bearish", "trend_up") == 0.0
        assert signal_weight_multiplier("rsi_extreme", "bearish", "trend_up") == 0.0

    def test_should_keep_allowlisted_bearish_countertrend(self):
        # double_top/bearish is universally promoted (1.6x) and allowlisted.
        assert signal_weight_multiplier("double_top", "bearish", "trend_up") == pytest.approx(1.6)

    def test_should_not_affect_with_trend_longs(self):
        assert signal_weight_multiplier("gap_up", "bullish", "trend_up") == pytest.approx(1.6)


class TestRegimePromoteSetCleanup:
    def test_should_not_promote_bearish_countertrend_in_uptrend(self):
        # The two removed entries must not have crept back in — they would be
        # promoted to 2.0x and re-introduce the bias.
        assert ("trend_up", "rsi_extreme", "bearish") not in REGIME_PROMOTE_SET
        assert ("trend_up", "evening_star", "bearish") not in REGIME_PROMOTE_SET

    def test_allowlist_contains_only_proven_countertrend_edge(self):
        assert ("trend_up", "double_top", "bearish") in COUNTERTREND_ALLOWLIST

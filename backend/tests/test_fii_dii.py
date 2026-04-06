from __future__ import annotations
"""Tests for app.services.fii_dii — FII/DII flow integration and signal modifiers."""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.fii_dii import (
    _classify_sentiment,
    _empty_result,
    get_fii_dii_data,
    get_signal_strength_modifier,
)


# ─────────────────────────────────────────────
# _classify_sentiment
# ─────────────────────────────────────────────

class TestClassifySentiment:
    def test_strong_buy_is_bullish(self):
        assert _classify_sentiment(2000.0) == "bullish"

    def test_exactly_threshold_is_bullish(self):
        assert _classify_sentiment(1500.0) == "bullish"

    def test_strong_sell_is_bearish(self):
        assert _classify_sentiment(-2000.0) == "bearish"

    def test_exactly_negative_threshold_is_bearish(self):
        assert _classify_sentiment(-1500.0) == "bearish"

    def test_neutral_between_thresholds(self):
        assert _classify_sentiment(0.0) == "neutral"
        assert _classify_sentiment(500.0) == "neutral"
        assert _classify_sentiment(-500.0) == "neutral"
        assert _classify_sentiment(1499.0) == "neutral"


# ─────────────────────────────────────────────
# _empty_result
# ─────────────────────────────────────────────

class TestEmptyResult:
    def test_has_required_keys(self):
        result = _empty_result()
        assert result["fii_net"] is None
        assert result["dii_net"] is None
        assert result["sentiment"] == "neutral"
        assert result["source"] == "unavailable"
        assert "date" in result


# ─────────────────────────────────────────────
# get_signal_strength_modifier
# ─────────────────────────────────────────────

class TestGetSignalStrengthModifier:
    def test_fii_selling_penalizes_bullish(self):
        fii_data = {"fii_net": -2000.0}
        assert get_signal_strength_modifier(fii_data, "bullish") == -2

    def test_fii_buying_penalizes_bearish(self):
        fii_data = {"fii_net": 2000.0}
        assert get_signal_strength_modifier(fii_data, "bearish") == -2

    def test_fii_selling_does_not_penalize_bearish(self):
        fii_data = {"fii_net": -2000.0}
        assert get_signal_strength_modifier(fii_data, "bearish") == 0

    def test_fii_buying_does_not_penalize_bullish(self):
        fii_data = {"fii_net": 2000.0}
        assert get_signal_strength_modifier(fii_data, "bullish") == 0

    def test_neutral_fii_no_modifier(self):
        fii_data = {"fii_net": 500.0}
        assert get_signal_strength_modifier(fii_data, "bullish") == 0
        assert get_signal_strength_modifier(fii_data, "bearish") == 0

    def test_missing_fii_net_returns_zero(self):
        assert get_signal_strength_modifier({}, "bullish") == 0
        assert get_signal_strength_modifier({"fii_net": None}, "bullish") == 0

    def test_neutral_direction_not_penalized(self):
        fii_data = {"fii_net": -3000.0}
        assert get_signal_strength_modifier(fii_data, "neutral") == 0


# ─────────────────────────────────────────────
# get_fii_dii_data (async, with mocks)
# ─────────────────────────────────────────────

class TestGetFiiDiiData:
    @pytest.mark.asyncio
    async def test_returns_cached_data_if_available(self):
        cached = {"fii_net": 1000.0, "sentiment": "neutral", "source": "nse"}
        with patch("app.services.fii_dii.cache_manager") as mock_cache:
            mock_cache.get = AsyncMock(return_value=cached)
            result = await get_fii_dii_data()
        assert result["fii_net"] == 1000.0
        assert result["source"] == "nse"

    @pytest.mark.asyncio
    async def test_falls_back_to_empty_on_fetch_failure(self):
        with patch("app.services.fii_dii.cache_manager") as mock_cache:
            mock_cache.get = AsyncMock(return_value=None)
            mock_cache.set = AsyncMock()
            with patch("app.services.fii_dii._fetch_from_nse", new=AsyncMock(return_value=None)):
                result = await get_fii_dii_data()
        assert result["fii_net"] is None
        assert result["source"] == "unavailable"

    @pytest.mark.asyncio
    async def test_caches_successful_fetch(self):
        fetched = {"fii_net": -1800.0, "dii_net": 1200.0, "fii_5d_avg": -1800.0,
                    "sentiment": "bearish", "source": "nse", "date": "2026-04-06"}
        with patch("app.services.fii_dii.cache_manager") as mock_cache:
            mock_cache.get = AsyncMock(return_value=None)
            mock_cache.set = AsyncMock()
            with patch("app.services.fii_dii._fetch_from_nse", new=AsyncMock(return_value=fetched)):
                result = await get_fii_dii_data()
        assert result["fii_net"] == -1800.0
        mock_cache.set.assert_awaited_once()

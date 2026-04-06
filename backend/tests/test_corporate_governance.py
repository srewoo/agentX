from __future__ import annotations
"""Tests for app.services.corporate_governance — promoter pledge risk assessment."""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.corporate_governance import (
    _classify_pledge_risk,
    _empty_pledge_result,
    get_pledge_strength_modifier,
    get_promoter_pledge_data,
)


# ─────────────────────────────────────────────
# _classify_pledge_risk
# ─────────────────────────────────────────────

class TestClassifyPledgeRisk:
    def test_critical_above_75(self):
        assert _classify_pledge_risk(80.0) == "critical"

    def test_high_50_to_75(self):
        assert _classify_pledge_risk(55.0) == "high"

    def test_medium_25_to_50(self):
        assert _classify_pledge_risk(30.0) == "medium"

    def test_low_below_25(self):
        assert _classify_pledge_risk(10.0) == "low"

    def test_zero_is_low(self):
        assert _classify_pledge_risk(0.0) == "low"

    def test_none_is_unknown(self):
        assert _classify_pledge_risk(None) == "unknown"

    def test_boundary_75_is_critical(self):
        assert _classify_pledge_risk(75.0) == "critical"

    def test_boundary_50_is_high(self):
        assert _classify_pledge_risk(50.0) == "high"

    def test_boundary_25_is_medium(self):
        assert _classify_pledge_risk(25.0) == "medium"


# ─────────────────────────────────────────────
# _empty_pledge_result
# ─────────────────────────────────────────────

class TestEmptyPledgeResult:
    def test_has_required_fields(self):
        result = _empty_pledge_result("RELIANCE")
        assert result["symbol"] == "RELIANCE"
        assert result["promoter_holding_pct"] is None
        assert result["pledged_pct"] is None
        assert result["risk_level"] == "unknown"
        assert result["source"] == "unavailable"


# ─────────────────────────────────────────────
# get_pledge_strength_modifier
# ─────────────────────────────────────────────

class TestGetPledgeStrengthModifier:
    def test_critical_pledge_minus_3(self):
        assert get_pledge_strength_modifier({"risk_level": "critical"}) == -3

    def test_high_pledge_minus_2(self):
        assert get_pledge_strength_modifier({"risk_level": "high"}) == -2

    def test_medium_pledge_minus_1(self):
        assert get_pledge_strength_modifier({"risk_level": "medium"}) == -1

    def test_low_pledge_no_modifier(self):
        assert get_pledge_strength_modifier({"risk_level": "low"}) == 0

    def test_unknown_no_modifier(self):
        assert get_pledge_strength_modifier({"risk_level": "unknown"}) == 0

    def test_missing_key_no_modifier(self):
        assert get_pledge_strength_modifier({}) == 0


# ─────────────────────────────────────────────
# get_promoter_pledge_data (async, with mocks)
# ─────────────────────────────────────────────

class TestGetPromoterPledgeData:
    @pytest.mark.asyncio
    async def test_returns_cached_data(self):
        cached = {"symbol": "ADANIENT", "pledged_pct": 60.0, "risk_level": "high"}
        with patch("app.services.corporate_governance.cache_manager") as mock_cache:
            mock_cache.get = AsyncMock(return_value=cached)
            result = await get_promoter_pledge_data("ADANIENT")
        assert result["pledged_pct"] == 60.0
        assert result["risk_level"] == "high"

    @pytest.mark.asyncio
    async def test_falls_back_to_empty_on_failure(self):
        with patch("app.services.corporate_governance.cache_manager") as mock_cache:
            mock_cache.get = AsyncMock(return_value=None)
            mock_cache.set = AsyncMock()
            with patch("app.services.corporate_governance._fetch_pledge_data", new=AsyncMock(return_value=None)):
                result = await get_promoter_pledge_data("XYZ")
        assert result["symbol"] == "XYZ"
        assert result["source"] == "unavailable"

    @pytest.mark.asyncio
    async def test_caches_successful_fetch(self):
        fetched = {"symbol": "TCS", "promoter_holding_pct": 72.0, "pledged_pct": 5.0,
                    "pledged_of_total_pct": 3.6, "risk_level": "low", "source": "nse"}
        with patch("app.services.corporate_governance.cache_manager") as mock_cache:
            mock_cache.get = AsyncMock(return_value=None)
            mock_cache.set = AsyncMock()
            with patch("app.services.corporate_governance._fetch_pledge_data", new=AsyncMock(return_value=fetched)):
                result = await get_promoter_pledge_data("TCS")
        assert result["pledged_pct"] == 5.0
        mock_cache.set.assert_awaited_once()

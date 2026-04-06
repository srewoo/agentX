from __future__ import annotations
"""Tests for app.services.market_rules — circuit limits, F&O ban, signal suppression."""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.market_rules import (
    is_near_circuit,
    should_suppress_signal,
    get_fno_ban_list,
)


# ─────────────────────────────────────────────
# is_near_circuit
# ─────────────────────────────────────────────

class TestIsNearCircuit:
    @pytest.mark.asyncio
    async def test_normal_move_no_circuit(self):
        # 2% move is below all circuit warning zones (5%-2%=3%, 10%-2%=8%, 20%-2%=18%)
        result = await is_near_circuit("TCS", current_price=102.0, prev_close=100.0)
        assert result["near_upper_circuit"] is False
        assert result["near_lower_circuit"] is False

    @pytest.mark.asyncio
    async def test_near_5pct_upper_circuit(self):
        # 4% move → within 2% of the 5% circuit
        result = await is_near_circuit("PENNY", current_price=104.0, prev_close=100.0)
        assert result["near_upper_circuit"] is True
        assert result["warning"] is not None
        assert "up" in result["warning"].lower()

    @pytest.mark.asyncio
    async def test_near_20pct_upper_circuit(self):
        result = await is_near_circuit("X", current_price=119.0, prev_close=100.0)
        assert result["near_upper_circuit"] is True

    @pytest.mark.asyncio
    async def test_near_lower_circuit(self):
        result = await is_near_circuit("Y", current_price=92.0, prev_close=100.0)
        assert result["near_lower_circuit"] is True
        assert "down" in result["warning"].lower()

    @pytest.mark.asyncio
    async def test_zero_prev_close_safe(self):
        result = await is_near_circuit("Z", current_price=100.0, prev_close=0.0)
        assert result["near_upper_circuit"] is False
        assert result["near_lower_circuit"] is False

    @pytest.mark.asyncio
    async def test_moderate_move_no_warning(self):
        result = await is_near_circuit("A", current_price=102.0, prev_close=100.0)
        assert result["warning"] is None


# ─────────────────────────────────────────────
# should_suppress_signal
# ─────────────────────────────────────────────

class TestShouldSuppressSignal:
    def test_banned_stock_suppressed(self):
        suppress, reason = should_suppress_signal("RBLBANK", {"RBLBANK", "IBULHSGFIN"})
        assert suppress is True
        assert "F&O ban" in reason

    def test_non_banned_stock_not_suppressed(self):
        suppress, reason = should_suppress_signal("RELIANCE", {"RBLBANK"})
        assert suppress is False

    def test_near_circuit_warns_but_does_not_suppress(self):
        circuit_info = {"near_upper_circuit": True, "warning": "Stock up 19.5%"}
        suppress, reason = should_suppress_signal("X", set(), circuit_info=circuit_info)
        assert suppress is False
        assert "19.5" in reason

    def test_no_circuit_info_no_suppression(self):
        suppress, reason = should_suppress_signal("Y", set())
        assert suppress is False
        assert reason == ""

    def test_empty_ban_list(self):
        suppress, _ = should_suppress_signal("RELIANCE", set())
        assert suppress is False


# ─────────────────────────────────────────────
# get_fno_ban_list (async, with mocks)
# ─────────────────────────────────────────────

class TestGetFnoBanList:
    @pytest.mark.asyncio
    async def test_returns_cached_if_available(self):
        with patch("app.services.market_rules.cache_manager") as mock_cache:
            mock_cache.get = AsyncMock(return_value=["RBLBANK", "IBULHSGFIN"])
            result = await get_fno_ban_list()
        assert isinstance(result, set)
        assert "RBLBANK" in result

    @pytest.mark.asyncio
    async def test_returns_empty_on_fetch_failure(self):
        with patch("app.services.market_rules.cache_manager") as mock_cache:
            mock_cache.get = AsyncMock(return_value=None)
            mock_cache.set = AsyncMock()
            with patch("app.services.market_rules._fetch_fno_ban_from_nse", new=AsyncMock(return_value=None)):
                result = await get_fno_ban_list()
        assert result == set()

    @pytest.mark.asyncio
    async def test_caches_successful_fetch(self):
        ban_set = {"RBLBANK"}
        with patch("app.services.market_rules.cache_manager") as mock_cache:
            mock_cache.get = AsyncMock(return_value=None)
            mock_cache.set = AsyncMock()
            with patch("app.services.market_rules._fetch_fno_ban_from_nse", new=AsyncMock(return_value=ban_set)):
                result = await get_fno_ban_list()
        assert "RBLBANK" in result
        mock_cache.set.assert_awaited_once()

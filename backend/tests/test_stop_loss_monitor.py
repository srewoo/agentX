"""Tests for the decoupled stop-loss monitor."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.stop_loss_monitor import (
    _is_breached,
    evaluate_open_positions,
)


# ── Direction-aware breach test (pure) ────────────────────────────────────

def test_bullish_breach_when_price_at_or_below_stop():
    assert _is_breached("bullish", current=99.0, stop=100.0) is True
    assert _is_breached("bullish", current=100.0, stop=100.0) is True
    assert _is_breached("bullish", current=100.01, stop=100.0) is False


def test_bearish_breach_when_price_at_or_above_stop():
    assert _is_breached("bearish", current=101.0, stop=100.0) is True
    assert _is_breached("bearish", current=100.0, stop=100.0) is True
    assert _is_breached("bearish", current=99.99, stop=100.0) is False


def test_zero_stop_never_breaches():
    assert _is_breached("bullish", current=99.0, stop=0) is False


def test_unknown_direction_never_breaches():
    assert _is_breached("sideways", current=50, stop=100) is False


# ── Async evaluation against a mocked price ──────────────────────────────

@pytest.mark.asyncio
async def test_evaluate_skips_position_with_no_price():
    positions = [{"id": "1", "symbol": "RELIANCE", "direction": "bullish", "stop_loss": 100}]
    with patch("app.services.stop_loss_monitor._current_price", new=AsyncMock(return_value=None)):
        out = await evaluate_open_positions(positions)
    assert out == []


@pytest.mark.asyncio
async def test_evaluate_flags_breach():
    positions = [
        {"id": "1", "symbol": "RELIANCE", "direction": "bullish",
         "entry_price": 110, "stop_loss": 100, "shares": 10},
        {"id": "2", "symbol": "ITC", "direction": "bullish",
         "entry_price": 200, "stop_loss": 180, "shares": 5},
    ]
    async def fake_price(symbol: str):
        return 95.0 if symbol == "RELIANCE" else 195.0

    with patch("app.services.stop_loss_monitor._current_price", new=AsyncMock(side_effect=fake_price)):
        out = await evaluate_open_positions(positions)

    assert len(out) == 1
    assert out[0]["symbol"] == "RELIANCE"
    assert out[0]["trigger_price"] == 95.0
    assert "Stop-loss triggered" in out[0]["reason"]


@pytest.mark.asyncio
async def test_evaluate_normalises_broker_side_strings():
    """BUY/SELL → bullish/bearish so broker payloads work without rewriting."""
    positions = [
        {"id": "1", "symbol": "X", "side": "SELL", "entry_price": 100,
         "stop_loss": 110, "shares": 10},
    ]
    with patch("app.services.stop_loss_monitor._current_price", new=AsyncMock(return_value=115.0)):
        out = await evaluate_open_positions(positions)
    # bearish position with stop above entry, price 115 ≥ 110 → breached
    assert len(out) == 1

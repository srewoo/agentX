"""Tests for the deterministic risk-gate + daily-loss circuit breaker."""
from __future__ import annotations

import os
import tempfile

import pytest

from app.services import risk_gate
from app.services.risk_gate import (
    GateVerdict,
    PortfolioState,
    TradeCandidate,
    can_open_new_trade,
    evaluate_trade,
    get_daily_pnl,
    record_trade_pnl,
)


# ── Daily-loss circuit breaker (uses real aiosqlite against tmp DB) ───────

@pytest.fixture
def tmp_db(monkeypatch):
    """Point the gate at a throwaway SQLite file so tests don't pollute prod."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(risk_gate, "DB_PATH", path)
    yield path
    os.remove(path)


@pytest.mark.asyncio
async def test_first_call_returns_zero_pnl(tmp_db):
    assert await get_daily_pnl() == 0.0


@pytest.mark.asyncio
async def test_record_pnl_accumulates(tmp_db):
    await record_trade_pnl(-500.0)
    await record_trade_pnl(-300.0)
    assert await get_daily_pnl() == -800.0


@pytest.mark.asyncio
async def test_circuit_breaker_blocks_after_threshold(tmp_db):
    capital = 100_000
    # 2.5% of 100k = 2500 → breaker fires at -2500
    await record_trade_pnl(-2_600.0)
    ok, reason = await can_open_new_trade(capital=capital)
    assert ok is False
    assert "circuit-breaker" in reason


@pytest.mark.asyncio
async def test_circuit_breaker_allows_below_threshold(tmp_db):
    await record_trade_pnl(-1_000.0)
    ok, _ = await can_open_new_trade(capital=100_000)
    assert ok is True


# ── Hard gate validator ───────────────────────────────────────────────────

def _basic_candidate(**overrides) -> TradeCandidate:
    base = dict(
        symbol="RELIANCE",
        direction="bullish",
        entry=2_400.0,
        stop=2_350.0,
        target=2_500.0,   # 100 reward vs 50 risk → R:R = 2.0
        qty=10,
        sector="Energy",
        avg_daily_volume=1_000_000,
        is_fno_banned=False,
        is_in_earnings_blackout=False,
    )
    base.update(overrides)
    return TradeCandidate(**base)


def _portfolio(**overrides) -> PortfolioState:
    base = dict(capital=1_000_000, open_positions=[], correlation_to_open=0.0)
    base.update(overrides)
    return PortfolioState(**base)


def test_clean_trade_is_approved():
    res = evaluate_trade(_basic_candidate(), _portfolio(), daily_pnl=0)
    assert res.verdict is GateVerdict.APPROVED


def test_fno_ban_rejects():
    res = evaluate_trade(_basic_candidate(is_fno_banned=True), _portfolio())
    assert res.verdict is GateVerdict.REJECTED
    assert any("ban" in r for r in res.reasons)


def test_earnings_blackout_rejects():
    res = evaluate_trade(_basic_candidate(is_in_earnings_blackout=True), _portfolio())
    assert res.verdict is GateVerdict.REJECTED
    assert any("earnings" in r for r in res.reasons)


def test_low_liquidity_rejects():
    res = evaluate_trade(_basic_candidate(avg_daily_volume=5_000), _portfolio())
    assert res.verdict is GateVerdict.REJECTED
    assert any("illiquid" in r for r in res.reasons)


def test_low_rr_rejects():
    # Set target so close that R:R < 1.5
    cand = _basic_candidate(target=2_410.0)  # 10 reward / 50 risk = 0.2
    res = evaluate_trade(cand, _portfolio())
    assert res.verdict is GateVerdict.REJECTED
    assert any("R:R" in r for r in res.reasons)


def test_daily_loss_breaker_blocks_inside_gate():
    res = evaluate_trade(
        _basic_candidate(),
        _portfolio(capital=100_000),
        daily_pnl=-3_000.0,   # > 2.5% threshold of 100k
    )
    assert res.verdict is GateVerdict.REJECTED
    assert any("circuit breaker" in r for r in res.reasons)


def test_max_open_positions_blocks():
    p = _portfolio(open_positions=[{"symbol": f"S{i}"} for i in range(10)])
    res = evaluate_trade(_basic_candidate(), p)
    assert res.verdict is GateVerdict.REJECTED
    assert any("max open positions" in r for r in res.reasons)


def test_sector_concentration_blocks():
    # Existing book already 30% in Energy
    p = _portfolio(open_positions=[
        {"symbol": "ONGC", "sector": "Energy", "shares": 100, "entry_price": 3_000}
    ])
    # capital=1_000_000; existing = 300_000 = 30% → over 25% cap
    res = evaluate_trade(_basic_candidate(qty=1), p)
    assert res.verdict is GateVerdict.REJECTED
    assert any("sector" in r for r in res.reasons)


def test_oversized_position_gets_modified_not_rejected():
    # qty=50 × 2400 = 1.2L on 10L capital → 12% → over 5% cap but
    # under the 25% sector cap so we hit the modify branch, not reject.
    cand = _basic_candidate(qty=50)
    res = evaluate_trade(cand, _portfolio())
    assert res.verdict is GateVerdict.MODIFIED
    assert res.modified_qty is not None and res.modified_qty < 50


def test_high_correlation_emits_warning_not_reject():
    p = _portfolio(correlation_to_open=0.85)
    res = evaluate_trade(_basic_candidate(), p)
    # Approved but with a warning.
    assert res.verdict is GateVerdict.APPROVED
    assert any("correlation" in w for w in res.warnings)


# ── Quality gates (4a / 4b / 4c) ─────────────────────────────────────────

def test_bad_data_quality_rejects():
    res = evaluate_trade(_basic_candidate(data_quality="stale"), _portfolio())
    assert res.verdict is GateVerdict.REJECTED
    assert any("data quality" in r for r in res.reasons)


def test_good_data_quality_passes():
    res = evaluate_trade(_basic_candidate(data_quality="ok"), _portfolio())
    assert res.verdict is GateVerdict.APPROVED


def test_data_quality_none_is_inert():
    # No data_quality supplied ⇒ gate must not fire.
    res = evaluate_trade(_basic_candidate(data_quality=None), _portfolio())
    assert res.verdict is GateVerdict.APPROVED


def test_wide_spread_rejects():
    # bid 100 / ask 102 → 2% spread > 1% cap.
    res = evaluate_trade(_basic_candidate(bid=100.0, ask=102.0), _portfolio())
    assert res.verdict is GateVerdict.REJECTED
    assert any("spread" in r for r in res.reasons)


def test_tight_spread_passes():
    # bid 100 / ask 100.2 → 0.2% spread, under cap.
    res = evaluate_trade(_basic_candidate(bid=100.0, ask=100.2), _portfolio())
    assert res.verdict is GateVerdict.APPROVED


def test_spread_inert_without_quotes():
    res = evaluate_trade(_basic_candidate(bid=None, ask=None), _portfolio())
    assert res.verdict is GateVerdict.APPROVED


def test_atr_chop_rejects():
    # High ATR (6%) + low ADX (15) → choppy, no trend.
    res = evaluate_trade(_basic_candidate(atr_pct=6.0, adx=15.0), _portfolio())
    assert res.verdict is GateVerdict.REJECTED
    assert any("choppy" in r for r in res.reasons)


def test_high_atr_with_strong_trend_passes():
    # High ATR but strong ADX (35) is a trending move, not chop.
    res = evaluate_trade(_basic_candidate(atr_pct=6.0, adx=35.0), _portfolio())
    assert res.verdict is GateVerdict.APPROVED


def test_atr_chop_inert_without_inputs():
    res = evaluate_trade(_basic_candidate(atr_pct=None, adx=None), _portfolio())
    assert res.verdict is GateVerdict.APPROVED

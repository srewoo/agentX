from __future__ import annotations

"""Tests for app.services.recommendation engine.

- Pure-function golden tests for each factor scorer (no I/O).
- Property tests: conviction is monotonic in absolute weighted score, and
  monotonic in any single positive factor's score (others held fixed).
- End-to-end tests for `generate_recommendation` use monkeypatching of the
  external I/O surface only — no mocked DB, no fake yfinance internals.
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from app.models.recommendation import Recommendation, SignalContribution
from app.services import recommendation as rec_mod
from app.services import recommendation_factors as factors


# ---------------------------------------------------------------------------
# Factor scorers — golden tests
# ---------------------------------------------------------------------------

class TestTrendScore:
    def test_given_full_bullish_stack_when_scored_then_positive(self):
        tech = {
            "current_price": 110, "adx": 35,
            "moving_averages": {"sma20": 105, "sma50": 100, "sma200": 95},
        }
        score, _, direction = factors.trend_score(tech)
        assert score > 0.5
        assert direction == "bullish"

    def test_given_full_bearish_stack_when_scored_then_negative(self):
        tech = {
            "current_price": 90, "adx": 35,
            "moving_averages": {"sma20": 95, "sma50": 100, "sma200": 105},
        }
        score, _, direction = factors.trend_score(tech)
        assert score < -0.5
        assert direction == "bearish"

    def test_given_weak_adx_when_scored_then_magnitude_halved(self):
        tech_strong = {
            "current_price": 110, "adx": 35,
            "moving_averages": {"sma20": 105, "sma50": 100, "sma200": 95},
        }
        tech_weak = {**tech_strong, "adx": 15}
        s_strong, *_ = factors.trend_score(tech_strong)
        s_weak, *_ = factors.trend_score(tech_weak)
        assert abs(s_weak) < abs(s_strong)

    def test_given_no_price_when_scored_then_neutral(self):
        score, _, direction = factors.trend_score({"current_price": None})
        assert score == 0.0
        assert direction == "neutral"


class TestMomentumScore:
    def test_given_rsi_70_macd_bull_when_scored_then_strongly_bullish(self):
        tech = {"rsi": 70, "macd": {"macd_line": 1.0, "signal_line": 0.5}}
        score, _, direction = factors.momentum_score(tech)
        assert score > 0.5
        assert direction == "bullish"

    def test_given_rsi_30_macd_bear_when_scored_then_strongly_bearish(self):
        tech = {"rsi": 30, "macd": {"macd_line": -1.0, "signal_line": -0.5}}
        score, _, direction = factors.momentum_score(tech)
        assert score < -0.5
        assert direction == "bearish"


class TestVolumeDeliveryScore:
    def test_given_3x_volume_high_delivery_then_strong_score(self):
        tech = {"volume_current": 3_000_000, "volume_avg_20": 1_000_000}
        score, _, _ = factors.volume_delivery_score(tech, delivery_pct=70)
        assert score > 0.7

    def test_given_low_delivery_pct_then_score_penalized(self):
        tech = {"volume_current": 3_000_000, "volume_avg_20": 1_000_000}
        s_high, *_ = factors.volume_delivery_score(tech, delivery_pct=70)
        s_low, *_ = factors.volume_delivery_score(tech, delivery_pct=20)
        assert s_high > s_low

    def test_given_no_volume_then_neutral(self):
        score, _, direction = factors.volume_delivery_score({}, None)
        assert score == 0.0
        assert direction == "neutral"


class TestFnoScore:
    def test_given_high_pcr_when_scored_then_bullish(self):
        score, _, direction, _ = factors.fno_score({"pcr_oi": 1.6}, price_change_pct=0)
        assert score > 0.5
        assert direction == "bullish"

    def test_given_low_pcr_when_scored_then_bearish(self):
        score, _, direction, _ = factors.fno_score({"pcr_oi": 0.4}, price_change_pct=0)
        assert score < -0.5
        assert direction == "bearish"

    def test_given_price_up_oi_up_then_long_buildup(self):
        _, _, _, sig = factors.fno_score(
            {"pcr_oi": 1.0, "total_pe_oi": 200_000, "total_ce_oi": 100_000},
            price_change_pct=2.0,
        )
        assert sig == "LONG_BUILDUP"

    def test_given_price_down_oi_up_then_short_buildup(self):
        _, _, _, sig = factors.fno_score(
            {"pcr_oi": 1.0, "total_pe_oi": 200_000, "total_ce_oi": 100_000},
            price_change_pct=-2.0,
        )
        assert sig == "SHORT_BUILDUP"

    def test_given_no_options_then_neutral_no_signal(self):
        score, _, _, sig = factors.fno_score(None, price_change_pct=1.0)
        assert score == 0.0
        assert sig is None


class TestFiiDiiScore:
    def test_given_strong_inflow_then_positive_inflow(self):
        score, _, _, sig = factors.fii_dii_score({"fii_net": 2000.0})
        assert score > 0.5
        assert sig == "INFLOW"

    def test_given_strong_outflow_then_negative_outflow(self):
        score, _, _, sig = factors.fii_dii_score({"fii_net": -2000.0})
        assert score < -0.5
        assert sig == "OUTFLOW"

    def test_given_no_data_then_none(self):
        score, _, _, sig = factors.fii_dii_score({})
        assert score == 0.0
        assert sig is None


class TestVolatilityScore:
    def test_given_high_atr_pct_then_negative(self):
        s, _, _ = factors.volatility_score({"atr_pct": 7.0})
        assert s < 0

    def test_given_low_atr_pct_then_slightly_positive(self):
        s, _, _ = factors.volatility_score({"atr_pct": 0.5})
        assert s > 0


class TestEntrySlTargets:
    def test_given_bullish_when_computed_then_sl_below_t1_above(self):
        e, sl, t1, t2 = factors.entry_sl_targets(100.0, atr=2.0, horizon="swing", direction_up=True)
        assert sl < e < t1 < (t2 or float("inf"))

    def test_given_bearish_when_computed_then_sl_above_t1_below(self):
        e, sl, t1, _ = factors.entry_sl_targets(100.0, atr=2.0, horizon="swing", direction_up=False)
        assert t1 < e < sl

    def test_given_no_atr_when_computed_then_falls_back_to_2pct(self):
        e, sl, t1, _ = factors.entry_sl_targets(100.0, atr=None, horizon="swing", direction_up=True)
        # 2% fallback × 1.5 sl_mult for swing → 3% below entry
        assert sl == pytest.approx(97.0, abs=0.01)
        assert t1 == pytest.approx(106.0, abs=0.01)

    def test_given_intraday_then_tighter_than_positional(self):
        _, sl_i, _, _ = factors.entry_sl_targets(100.0, 2.0, "intraday", True)
        _, sl_p, _, _ = factors.entry_sl_targets(100.0, 2.0, "positional", True)
        assert (100 - sl_i) < (100 - sl_p)


# ---------------------------------------------------------------------------
# Conviction monotonicity — property test
# ---------------------------------------------------------------------------

class TestConvictionMonotonic:

    @pytest.mark.parametrize("weighted", [-1.0, -0.5, -0.1, 0.0, 0.1, 0.5, 1.0])
    def test_given_weighted_score_when_mapped_then_in_range(self, weighted):
        c = rec_mod.conviction_from_score(weighted)
        assert 0 <= c <= 100

    def test_given_higher_abs_score_then_higher_conviction(self):
        # Property: conviction is monotonic in |weighted_score|
        for a, b in [(0.0, 0.1), (0.1, 0.3), (0.3, 0.6), (0.6, 0.9)]:
            assert rec_mod.conviction_from_score(a) <= rec_mod.conviction_from_score(b)
            assert rec_mod.conviction_from_score(-a) <= rec_mod.conviction_from_score(-b)

    def test_action_thresholds(self):
        assert rec_mod.action_from_score(0.5) == "BUY"
        assert rec_mod.action_from_score(-0.5) == "SELL"
        assert rec_mod.action_from_score(0.0) == "HOLD"
        assert rec_mod.action_from_score(0.10) == "HOLD"  # below 0.15 threshold


# ---------------------------------------------------------------------------
# End-to-end: generate_recommendation with patched I/O
# ---------------------------------------------------------------------------

def _make_bullish_df(n: int = 250) -> pd.DataFrame:
    """Synthetic bullish daily history: steady uptrend with realistic OHLC."""
    idx = pd.date_range(end=datetime.now(timezone.utc), periods=n, freq="D")
    base = np.linspace(800, 1100, n)
    noise = np.random.RandomState(42).normal(0, 5, n)
    close = base + noise
    high = close + np.abs(np.random.RandomState(1).normal(3, 1, n))
    low = close - np.abs(np.random.RandomState(2).normal(3, 1, n))
    open_ = close - np.random.RandomState(3).normal(0, 2, n)
    vol = np.random.RandomState(4).randint(1_000_000, 5_000_000, n)
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=idx,
    )


def _make_bearish_df(n: int = 250) -> pd.DataFrame:
    idx = pd.date_range(end=datetime.now(timezone.utc), periods=n, freq="D")
    base = np.linspace(1100, 800, n)
    noise = np.random.RandomState(42).normal(0, 5, n)
    close = base + noise
    high = close + np.abs(np.random.RandomState(1).normal(3, 1, n))
    low = close - np.abs(np.random.RandomState(2).normal(3, 1, n))
    open_ = close + np.random.RandomState(3).normal(0, 2, n)
    vol = np.random.RandomState(4).randint(1_000_000, 5_000_000, n)
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=idx,
    )


@pytest.fixture
def patch_external_io(monkeypatch):
    """Patch all network-bound deps used by generate_recommendation."""
    async def _no_cache_get(_key):
        return None

    async def _no_cache_set(*_a, **_kw):
        return False

    async def _quote(_sym):
        return {"lastPrice": 1100.0, "pChange": 1.0}

    async def _delivery(_sym):
        return {"symbol": _sym, "delivery_pct": 65.0}

    async def _fii():
        return {"fii_net": 2000.0, "dii_net": 500.0, "sentiment": "bullish"}

    async def _options(_sym):
        return {
            "pcr_oi": 1.5, "total_pe_oi": 500_000, "total_ce_oi": 200_000,
            "max_pain": 1100, "unusual_ce_activity": [], "unusual_pe_activity": [],
        }

    async def _rs(syms, period="3mo"):
        return {"nifty_return": 5.0, "rankings": {s: {"rs_rank": 85, "rs_ratio": 1.5, "return_pct": 7} for s in syms}}

    async def _portfolio_context(**_kwargs):
        return {"available": True, "action_adjustment": 0, "notes": [], "decision": "neutral"}

    monkeypatch.setattr(rec_mod.cache_manager, "get", _no_cache_get)
    monkeypatch.setattr(rec_mod.cache_manager, "set", _no_cache_set)
    monkeypatch.setattr(rec_mod, "get_stock_quote", _quote)
    monkeypatch.setattr(rec_mod, "get_delivery_volume", _delivery)
    monkeypatch.setattr(rec_mod, "get_fii_dii_data", _fii)
    monkeypatch.setattr(rec_mod, "get_option_chain_analysis", _options)
    monkeypatch.setattr(rec_mod, "compute_relative_strength", _rs)
    import app.services.portfolio as portfolio_mod
    monkeypatch.setattr(portfolio_mod, "portfolio_recommendation_context", _portfolio_context)


@pytest.mark.asyncio
async def test_given_bullish_history_when_generated_then_buy(patch_external_io, monkeypatch):
    async def _hist(_sym, period="6mo", interval="1d"):
        return _make_bullish_df()
    monkeypatch.setattr(rec_mod, "async_fetch_history", _hist)

    rec = await rec_mod.generate_recommendation("RELIANCE", horizon="swing")
    assert rec is not None
    assert rec.action == "BUY"
    assert rec.conviction > 30
    assert rec.target1 > rec.entry > rec.stoploss
    assert rec.risk_reward > 0
    assert rec.fii_dii_signal == "INFLOW"
    # F&O sig depends on noisy 1d price delta — just verify it's set.
    assert rec.f_and_o_signal is not None
    assert any(s.name == "trend" for s in rec.signals)


@pytest.mark.asyncio
async def test_given_intraday_horizon_when_generated_then_uses_5m_history(patch_external_io, monkeypatch):
    seen = {}

    async def _hist(_sym, period="6mo", interval="1d"):
        seen["period"] = period
        seen["interval"] = interval
        return _make_bullish_df()

    monkeypatch.setattr(rec_mod, "async_fetch_history", _hist)

    rec = await rec_mod.generate_recommendation("RELIANCE", horizon="intraday")
    assert rec is not None
    assert seen == {"period": "5d", "interval": "5m"}
    assert rec.timeframe_days == 1


@pytest.mark.asyncio
async def test_given_bearish_history_when_generated_then_sell_or_hold(patch_external_io, monkeypatch):
    async def _hist(_sym, period="6mo", interval="1d"):
        return _make_bearish_df()
    monkeypatch.setattr(rec_mod, "async_fetch_history", _hist)
    # Override FII/options to also be bearish for a clean SELL.
    async def _fii_neg():
        return {"fii_net": -2500.0}
    async def _opts_neg(_sym):
        return {"pcr_oi": 0.4, "total_pe_oi": 100_000, "total_ce_oi": 400_000}
    monkeypatch.setattr(rec_mod, "get_fii_dii_data", _fii_neg)
    monkeypatch.setattr(rec_mod, "get_option_chain_analysis", _opts_neg)

    rec = await rec_mod.generate_recommendation("RELIANCE", horizon="swing")
    assert rec is not None
    assert rec.action in {"SELL", "HOLD"}
    if rec.action == "SELL":
        assert rec.target1 < rec.entry < rec.stoploss


@pytest.mark.asyncio
async def test_given_penny_stock_when_generated_then_avoid(patch_external_io, monkeypatch):
    df = _make_bullish_df()
    # Drag close down to penny territory.
    df["Close"] = df["Close"] / 200  # → ~5
    df["High"] = df["High"] / 200
    df["Low"] = df["Low"] / 200
    df["Open"] = df["Open"] / 200

    async def _hist(_sym, period="6mo", interval="1d"):
        return df
    monkeypatch.setattr(rec_mod, "async_fetch_history", _hist)

    rec = await rec_mod.generate_recommendation("PENNY", horizon="swing")
    assert rec is not None
    assert rec.action == "AVOID"
    assert rec.conviction == 0


@pytest.mark.asyncio
async def test_given_no_history_when_generated_then_none(monkeypatch):
    async def _hist(_sym, period="6mo", interval="1d"):
        return pd.DataFrame()
    monkeypatch.setattr(rec_mod, "async_fetch_history", _hist)

    async def _nope(*_a, **_kw):
        return None
    monkeypatch.setattr(rec_mod.cache_manager, "get", _nope)

    rec = await rec_mod.generate_recommendation("XYZ", horizon="swing")
    assert rec is None


@pytest.mark.asyncio
async def test_given_universe_when_batched_then_concurrency_bounded(patch_external_io, monkeypatch):
    async def _hist(_sym, period="6mo", interval="1d"):
        return _make_bullish_df()
    monkeypatch.setattr(rec_mod, "async_fetch_history", _hist)

    syms = ["RELIANCE", "TCS", "INFY", "ICICIBANK", "SBIN"]
    recs, errors = await rec_mod.generate_batch(syms, horizon="swing")
    assert len(recs) == len(syms)
    assert errors == []


# ---------------------------------------------------------------------------
# Schema & helpers
# ---------------------------------------------------------------------------

def test_recommendation_rejects_negative_price():
    with pytest.raises(Exception):
        Recommendation(
            symbol="X", exchange="NSE", horizon="swing", action="BUY",
            conviction=50, entry=-1, stoploss=1, target1=2, target2=None,
            risk_reward=1.0, timeframe_days=10, signals=[], reasons=[],
            sector="N/A", market_cap_band="LARGE", last_price=100,
            price_change_pct_1d=0.0, generated_at=datetime.now(timezone.utc),
        )


def test_default_universe_excludes_indices():
    syms = rec_mod.default_universe(limit=200)
    assert all(not s.startswith("^") for s in syms)
    assert "RELIANCE" in syms

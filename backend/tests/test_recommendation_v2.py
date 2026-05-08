"""Targeted unit tests for the v2 recommendation engine improvements:

  - news_sentiment_score
  - fundamentals_score
  - weekly_trend_score
  - _select_weights (regime switch)
  - _within_earnings_blackout
  - _diversify_by_sector

Pure-function tests only — no network, no fixtures from yfinance/NSE.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models.recommendation import Recommendation
from app.services.recommendation import (
    WEIGHTS_CALM,
    WEIGHTS_RISK_OFF,
    _diversify_by_sector,
    _select_weights,
    _within_earnings_blackout,
)
from app.services.recommendation_factors import (
    fundamentals_score,
    news_sentiment_score,
    weekly_trend_score,
)


# ── factor scorers ───────────────────────────────────────────────────────

class TestNewsSentiment:
    def test_empty_returns_zero(self):
        s, v, d = news_sentiment_score([])
        assert s == 0.0 and v is None and d == "neutral"

    def test_all_positive(self):
        articles = [{"sentiment_score": 0.6}, {"sentiment_score": 0.4}]
        s, v, d = news_sentiment_score(articles)
        assert s > 0 and d == "bullish"

    def test_all_negative(self):
        articles = [{"sentiment_score": -0.7}, {"sentiment_score": -0.5}]
        s, v, d = news_sentiment_score(articles)
        assert s < 0 and d == "bearish"

    def test_mixed_near_zero(self):
        articles = [{"sentiment_score": 0.2}, {"sentiment_score": -0.2}]
        s, v, d = news_sentiment_score(articles)
        assert abs(s) < 0.1 and d == "neutral"

    def test_clips_to_unit_range(self):
        articles = [{"sentiment_score": 1.0}] * 5
        s, _, _ = news_sentiment_score(articles)
        assert -1.0 <= s <= 1.0


class TestFundamentalsScore:
    def test_none_returns_zero(self):
        s, v, d = fundamentals_score(None)
        assert s == 0.0 and d == "neutral"

    def test_great_compounder(self):
        # PE 15, ROE 25%, low debt
        f = {
            "valuation": {"pe": 15.0},
            "profitability": {"roe": 0.25},
            "financial_health": {"debt_to_equity": 0.3},
        }
        s, _, d = fundamentals_score(f)
        assert s > 0.5 and d == "bullish"

    def test_junk_balance_sheet(self):
        # Negative ROE, high debt → strong negative.
        f = {
            "valuation": {"pe": 80.0},   # overvalued
            "profitability": {"roe": -0.05},
            "financial_health": {"debt_to_equity": 250.0},  # screener % style
        }
        s, _, d = fundamentals_score(f)
        assert s < -0.3 and d == "bearish"

    def test_handles_missing_branches(self):
        f = {"valuation": {"pe": 18.0}}
        s, _, _ = fundamentals_score(f)
        # Has only the valuation contribution, others fall through cleanly.
        assert -1.0 <= s <= 1.0


class TestWeeklyTrendScore:
    def test_none_returns_zero(self):
        s, _, _ = weekly_trend_score(None)
        assert s == 0.0

    def test_bullish_weekly_trend(self):
        # SMA20 < SMA50 < price, ADX strong → bullish.
        wkly = {
            "current_price": 110.0,
            "moving_averages": {"sma20": 105.0, "sma50": 100.0, "sma200": 90.0},
            "adx": 35,
        }
        s, _, d = weekly_trend_score(wkly)
        assert s > 0 and d == "bullish"


# ── regime weighting ─────────────────────────────────────────────────────

class TestSelectWeights:
    def test_no_vix_uses_calm(self):
        assert _select_weights(None) is WEIGHTS_CALM

    def test_low_vix_uses_calm(self):
        assert _select_weights(14.0) is WEIGHTS_CALM

    def test_high_vix_uses_risk_off(self):
        assert _select_weights(22.0) is WEIGHTS_RISK_OFF

    def test_each_profile_sums_to_one(self):
        for w in (WEIGHTS_CALM, WEIGHTS_RISK_OFF):
            assert abs(sum(w.values()) - 1.0) < 1e-9

    def test_risk_off_lowers_momentum_raises_fundamentals(self):
        assert WEIGHTS_RISK_OFF["momentum"] < WEIGHTS_CALM["momentum"]
        assert WEIGHTS_RISK_OFF["fundamentals"] >= WEIGHTS_CALM["fundamentals"]


# ── earnings blackout ────────────────────────────────────────────────────

class TestEarningsBlackout:
    def test_empty_actions_means_no_blackout(self):
        assert _within_earnings_blackout("PNB", [], days=5) is False

    def test_other_symbol_does_not_block(self):
        actions = [{
            "symbol": "RELIANCE",
            "action_type": "Quarterly Results",
            "ex_date": (datetime.now(timezone.utc).date()).isoformat(),
        }]
        assert _within_earnings_blackout("PNB", actions, days=5) is False

    def test_results_within_window_blocks(self):
        ex = datetime.now(timezone.utc).date() + timedelta(days=3)
        actions = [{
            "symbol": "PNB",
            "action_type": "Quarterly Results",
            "ex_date": ex.isoformat(),
        }]
        assert _within_earnings_blackout("PNB", actions, days=5) is True

    def test_results_beyond_window_do_not_block(self):
        ex = datetime.now(timezone.utc).date() + timedelta(days=20)
        actions = [{
            "symbol": "PNB",
            "action_type": "Quarterly Results",
            "ex_date": ex.isoformat(),
        }]
        assert _within_earnings_blackout("PNB", actions, days=5) is False

    def test_dividend_action_does_not_block(self):
        ex = datetime.now(timezone.utc).date() + timedelta(days=2)
        actions = [{
            "symbol": "PNB",
            "action_type": "Dividend",
            "ex_date": ex.isoformat(),
        }]
        assert _within_earnings_blackout("PNB", actions, days=5) is False


# ── sector diversification ───────────────────────────────────────────────

def _rec(symbol: str, sector: str, action: str = "BUY", conv: int = 70) -> Recommendation:
    """Build a minimal valid Recommendation for diversification tests."""
    return Recommendation(
        symbol=symbol, exchange="NSE", horizon="swing", action=action,
        conviction=conv, entry=100.0, stoploss=95.0, target1=110.0, target2=115.0,
        risk_reward=2.0, timeframe_days=10, signals=[],
        reasons=["test"], sector=sector, market_cap_band="LARGE",
        last_price=100.0, price_change_pct_1d=0.5,
        delivery_pct=None, fii_dii_signal=None, f_and_o_signal=None,
        generated_at=datetime.now(timezone.utc),
    )


class TestDiversifyBySector:
    def test_empty_input_returns_empty(self):
        assert _diversify_by_sector([]) == []

    def test_under_cap_passes_through_unchanged(self):
        recs = [_rec("HDFCBANK", "Banking"), _rec("ICICIBANK", "Banking")]
        out = _diversify_by_sector(recs)
        assert all(r.action == "BUY" for r in out)

    def test_over_cap_demotes_lowest_conviction(self):
        # 4 banks; cap is 2. Lowest-conviction 2 should become HOLD.
        recs = [
            _rec("HDFCBANK", "Banking", conv=90),
            _rec("ICICIBANK", "Banking", conv=80),
            _rec("AXISBANK", "Banking", conv=70),
            _rec("PNB", "Banking", conv=60),
        ]
        out = _diversify_by_sector(recs)
        actions = {r.symbol: r.action for r in out}
        assert actions["HDFCBANK"] == "BUY"
        assert actions["ICICIBANK"] == "BUY"
        assert actions["AXISBANK"] == "HOLD"
        assert actions["PNB"] == "HOLD"

    def test_does_not_touch_other_sectors(self):
        recs = [
            _rec("HDFCBANK", "Banking", conv=90),
            _rec("ICICIBANK", "Banking", conv=80),
            _rec("PNB", "Banking", conv=60),
            _rec("TCS", "IT", conv=50),
        ]
        out = _diversify_by_sector(recs)
        actions = {r.symbol: r.action for r in out}
        assert actions["TCS"] == "BUY"  # other sector untouched
        assert actions["PNB"] == "HOLD"  # demoted

    def test_avoid_remains_avoid(self):
        recs = [_rec("X", "Banking", action="AVOID", conv=0)] + [
            _rec(f"B{i}", "Banking", conv=90 - i) for i in range(3)
        ]
        out = _diversify_by_sector(recs)
        avoid = [r for r in out if r.symbol == "X"][0]
        assert avoid.action == "AVOID"

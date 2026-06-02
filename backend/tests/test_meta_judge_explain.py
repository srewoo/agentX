from __future__ import annotations
"""Tests for MetaJudge.explain — exact additive feature attribution."""
import pytest

from app.services.meta_judge import MetaJudge


def _training_trades():
    """A separable set: high momentum/trend ⇒ win, low ⇒ loss."""
    trades = []
    for i in range(30):
        trades.append({
            "momentum": 1.0, "trend": 1.0, "rsi": 60.0,
            "signal_type": "macd_divergence", "direction": "bullish",
            "regime": "trend_up", "symbol": "AAA",
            "win": True, "pnl": 2.0,
        })
        trades.append({
            "momentum": -1.0, "trend": -1.0, "rsi": 40.0,
            "signal_type": "double_top", "direction": "bearish",
            "regime": "trend_down", "symbol": "BBB",
            "win": False, "pnl": -2.0,
        })
    return trades


@pytest.fixture(scope="module")
def model() -> MetaJudge:
    return MetaJudge.train(
        _training_trades(), n_stumps=10, label_mode="win", enrich=False,
    )


def _winning_trade():
    return {"momentum": 1.0, "trend": 1.0, "rsi": 60.0,
            "signal_type": "macd_divergence", "direction": "bullish",
            "regime": "trend_up", "symbol": "AAA"}


def _losing_trade():
    return {"momentum": -1.0, "trend": -1.0, "rsi": 40.0,
            "signal_type": "double_top", "direction": "bearish",
            "regime": "trend_down", "symbol": "BBB"}


def test_contributions_sum_to_margin_exactly(model):
    """The defining property: Σ per-feature contributions == margin."""
    exp = model.explain(_winning_trade())
    total = sum(c["margin_contribution"] for c in exp["contributions"])
    assert total == pytest.approx(exp["margin"], abs=1e-5)


def test_explain_prob_matches_predict_proba(model):
    """explain() must reproduce the model's own probability."""
    t = _winning_trade()
    assert model.explain(t)["prob_win"] == pytest.approx(model.predict_proba(t), abs=1e-6)


def test_winner_scores_higher_than_loser(model):
    assert model.explain(_winning_trade())["prob_win"] > model.explain(_losing_trade())["prob_win"]


def test_contributions_sorted_by_abs_impact(model):
    contribs = model.explain(_winning_trade())["contributions"]
    mags = [abs(c["margin_contribution"]) for c in contribs]
    assert mags == sorted(mags, reverse=True)


def test_top_k_limits_output(model):
    full = model.explain(_winning_trade())
    limited = model.explain(_winning_trade(), top_k=1)
    assert len(limited["contributions"]) == 1
    assert len(full["contributions"]) >= len(limited["contributions"])


def test_direction_sign_matches_contribution(model):
    for c in model.explain(_winning_trade())["contributions"]:
        if c["margin_contribution"] > 0:
            assert c["direction"] == "bullish"
        elif c["margin_contribution"] < 0:
            assert c["direction"] == "bearish"


def test_empty_model_returns_base():
    empty = MetaJudge()
    exp = empty.explain(_winning_trade())
    assert exp["contributions"] == []
    assert exp["margin"] == 0.0
    assert 0.0 <= exp["prob_win"] <= 1.0

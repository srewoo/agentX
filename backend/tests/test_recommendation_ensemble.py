from __future__ import annotations

from app.models.recommendation import SignalContribution
from app.services.fundamental_valuation import analyze_fundamental_valuation
from app.services.recommendation_ensemble import build_recommendation_ensemble
from app.services.recommendation_llm_judge import validate_judge_output


def test_fundamental_valuation_rewards_quality_reasonable_price():
    out = analyze_fundamental_valuation(
        {
            "valuation": {"pe": 18.0, "pb": 3.0, "ev_ebitda": 12.0},
            "profitability": {"roe": 0.24, "profit_margin": 0.20, "operating_margin": 0.24},
            "growth": {"revenue_growth": 0.12, "earnings_growth": 0.18},
            "financial_health": {"debt_to_equity": 20.0},
            "dividends": {"dividend_yield": 0.015},
        },
        sector="IT",
    )
    assert out["available"] is True
    assert out["score"] >= 60
    assert out["normalized_score"] > 0
    assert out["grade"] in {"A", "B"}


def test_fundamental_valuation_flags_expensive_weak_business():
    out = analyze_fundamental_valuation(
        {
            "valuation": {"pe": 95.0, "pb": 12.0},
            "profitability": {"roe": -0.04, "profit_margin": -0.02},
            "growth": {"revenue_growth": -0.05, "earnings_growth": -0.20},
            "financial_health": {"debt_to_equity": 300.0},
            "dividends": {},
        },
        sector="IT",
    )
    assert out["score"] < 45
    assert out["red_flags"]


def test_ensemble_demotes_low_final_conviction_directional_call():
    ensemble = build_recommendation_ensemble(
        action="BUY",
        weighted_score=0.18,
        calibrated_conviction=42,
        factor_agreement=0.25,
        risk_reward=1.2,
        regime="neutral",
        contributions=[
            SignalContribution(name="trend", weight=0.2, value=30, score=0.2, direction="bullish"),
            SignalContribution(name="momentum", weight=0.2, value=55, score=-0.3, direction="bearish"),
        ],
        fundamental_valuation={"available": True, "score": 38, "grade": "D", "red_flags": ["Weak ROE."]},
        portfolio_context=None,
        data_quality="eod_verified",
    )
    assert ensemble["suggested_action"] == "HOLD"
    assert ensemble["final_conviction"] < 48


def test_ensemble_applies_llm_block_as_hold():
    ensemble = build_recommendation_ensemble(
        action="BUY",
        weighted_score=0.62,
        calibrated_conviction=72,
        factor_agreement=0.80,
        risk_reward=2.2,
        regime="trend_up",
        contributions=[
            SignalContribution(name="trend", weight=0.2, value=35, score=0.8, direction="bullish"),
        ],
        fundamental_valuation={"available": True, "score": 70, "grade": "B", "red_flags": []},
        portfolio_context=None,
        data_quality="eod_verified",
        llm_judge={"verdict": "BLOCK", "confidence_adjustment": -15},
    )
    assert ensemble["suggested_action"] == "HOLD"
    assert ensemble["blockers"]


def test_llm_judge_validation_clamps_adjustment_and_verdict():
    out = validate_judge_output({"verdict": "upgrade", "confidence_adjustment": 99, "summary": "ok"})
    assert out["verdict"] == "UPGRADE"
    assert out["confidence_adjustment"] == 15

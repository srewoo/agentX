from __future__ import annotations
"""C2–C4 — explainability pure functions."""
from app.services.recommendation_explain import (
    conviction_interval,
    counterfactual_swing_factor,
    enrich_factors,
    meta_judge_attribution,
)


# C2
def test_enrich_factors_attaches_evidence():
    contribs = [{"name": "trend", "weight": 0.2, "score": 0.5},
                {"name": "unknown_factor", "weight": 0.1, "score": 0.1}]
    snap = {"trend": {"win_rate": 54.0, "trades": 400}}
    out = enrich_factors(contribs, snap)
    assert out[0]["evidence"]["n"] == 400
    assert out[0]["evidence"]["win_rate"] == 54.0
    assert 0 < out[0]["evidence"]["wilson_lb"] < 54.0   # LB below point estimate
    assert out[1]["evidence"] is None                   # no fabricated edge


# C3
def test_conviction_interval_widens_on_thin_evidence():
    wide = conviction_interval(80, effective_n=10)
    narrow = conviction_interval(80, effective_n=1000)
    assert wide["width"] > narrow["width"]


def test_conviction_interval_no_evidence_is_full_range():
    ci = conviction_interval(80, effective_n=0)
    assert ci["low"] == 0 and ci["high"] == 100


# C4 counterfactual
def test_counterfactual_identifies_swing_factor():
    # Net positive score driven by one big factor; removing it flips negative.
    contribs = [
        {"name": "trend", "weight": 1.0, "score": 0.6},   # +0.6
        {"name": "momentum", "weight": 1.0, "score": -0.5},  # -0.5
    ]
    weighted = 0.6 - 0.5  # = +0.1 (a BUY-ish positive)
    swing = counterfactual_swing_factor(contribs, weighted, decision_threshold=0.0)
    assert swing is not None
    assert swing["factor"] == "trend"


def test_counterfactual_none_when_robust():
    contribs = [{"name": "a", "weight": 1.0, "score": 0.5},
                {"name": "b", "weight": 1.0, "score": 0.5}]
    # weighted +1.0; removing either single factor (+0.5) stays positive.
    assert counterfactual_swing_factor(contribs, 1.0, decision_threshold=0.0) is None


# C4 attribution
def test_meta_judge_attribution_ranks_features():
    stumps = [
        {"feature": "risk_reward", "threshold": 2.0, "polarity": 1, "alpha": 0.8},
        {"feature": "conviction", "threshold": 60, "polarity": 1, "alpha": 0.3},
        {"feature": "risk_reward", "threshold": 1.5, "polarity": 1, "alpha": 0.4},
    ]
    feats = {"risk_reward": 3.0, "conviction": 70}
    top = meta_judge_attribution(feats, stumps, top_k=2)
    assert top[0]["feature"] == "risk_reward"   # two stumps, highest |alpha| sum
    assert len(top) == 2

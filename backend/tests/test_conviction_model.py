from __future__ import annotations
"""2.4 — fitted conviction model: separable fit, beat-the-baseline gate, scale map."""
import pytest

from app.services import conviction_model as cm


# ── feature extraction ──
def test_risk_reward():
    assert cm.risk_reward(100, 95, 110) == pytest.approx(2.0)   # reward 10 / risk 5
    assert cm.risk_reward(100, 100, 110) == 0.0                 # degenerate risk


def test_extract_features_regime_onehot():
    f = cm.extract_features(weighted_score=-0.4, agreement=0.8, risk_reward=3.0,
                            regime="trend_down")
    assert f[0] == pytest.approx(0.4)      # magnitude (sign dropped)
    assert f[1] == pytest.approx(0.8)
    assert f[2] == pytest.approx(3.0)
    assert f[3] == 1.0 and f[4] == 0.0     # trend, not risk_off
    f2 = cm.extract_features(weighted_score=1.0, agreement=1.0, risk_reward=9.0,
                             regime="risk_off")
    assert f2[2] == 5.0                    # rr clamped
    assert f2[3] == 0.0 and f2[4] == 1.0


# ── logistic fit learns a separable signal ──
def test_fit_logistic_learns_separable_signal():
    # Higher score magnitude ⇒ win. Model must rank a strong setup above a weak one.
    X, y = [], []
    for i in range(200):
        strong = i % 2 == 0
        X.append([0.9 if strong else 0.1, 0.9 if strong else 0.3, 2.0, 1.0, 0.0])
        y.append(1 if strong else 0)
    b = cm.fit_logistic(X, y)
    p_strong = cm.predict_p(b, [0.9, 0.9, 2.0, 1.0, 0.0])
    p_weak = cm.predict_p(b, [0.1, 0.3, 2.0, 1.0, 0.0])
    assert p_strong > p_weak
    assert p_strong > 0.5 > p_weak


# ── beat-the-baseline gate ──
def test_evaluate_holdout_rewards_better_ranking():
    # Baseline keeps by conviction≥65; model p is a BETTER ranker (aligns with pnl).
    n = 10
    pnl = [5, 4, 3, 2, 1, -1, -2, -3, -4, -5]
    baseline_conv = [66, 50, 66, 50, 66, 66, 50, 66, 50, 66]   # keeps 6 mixed
    model_p = [0.9, 0.85, 0.8, 0.75, 0.7, 0.4, 0.3, 0.2, 0.1, 0.05]  # ranks winners top
    res = cm.evaluate_holdout(model_p, baseline_conv, pnl)
    assert res["kept_n"] == 6
    assert res["expectancy_lift"] > 0     # model's top-6 beats baseline's kept-6


def test_deploy_gate_fails_closed():
    assert cm.passes_deploy_gate(None)[0] is False
    assert cm.passes_deploy_gate({"holdout_n": 10, "kept_n": 3, "expectancy_lift": 1.0})[0] is False
    assert cm.passes_deploy_gate({"holdout_n": 50, "kept_n": 10, "expectancy_lift": -0.1})[0] is False
    ok, reason = cm.passes_deploy_gate({"holdout_n": 50, "kept_n": 10, "expectancy_lift": 0.5})
    assert ok is True and "beats baseline" in reason


# ── scale-preserving map ──
def test_conviction_map_preserves_scale_and_order():
    model_p = [0.2, 0.4, 0.5, 0.6, 0.8]
    baseline_conv = [40, 55, 60, 70, 85]
    cmap = cm.fit_conviction_map(model_p, baseline_conv)
    lo = cm.map_to_conviction(0.2, cmap)
    hi = cm.map_to_conviction(0.8, cmap)
    assert 0 <= lo <= 100 and 0 <= hi <= 100
    assert hi > lo                         # ordering preserved
    # Mean p maps to ~mean baseline conviction (moment match).
    assert abs(cm.map_to_conviction(0.5, cmap) - 62) <= 5


def test_model_conviction_none_when_not_deployed():
    cm._reset_cache()
    # No pickle / not deployed → None so caller keeps the multiplicative stack.
    assert cm.model_conviction(weighted_score=0.5, agreement=0.8,
                               risk_reward=2.0, regime="trend_up") is None

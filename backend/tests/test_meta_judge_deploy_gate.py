from __future__ import annotations
"""Regression: the meta-judge deploy gate must reject DEGENERATE models.

A model trained 2026-07-17 shipped with holdout keep-rate 4/109 (3.7%) and an
operating threshold of 0.99 — it kept ~0% of live signals and silently blocked
EVERY scan (0 signals, starving the paper-trader). The gate must reject any
model that keeps too little of its holdout or demands a near-certain
probability, falling back to mutes-only.
"""
from app.services.orchestrator import (
    _META_JUDGE_MIN_KEEP_RATE,
    _META_JUDGE_MAX_THRESHOLD,
)


def _deploy_decision(auc: float, n_kept: int, n_dropped: int, threshold: float) -> str:
    """Mirror of the deploy-gate logic in run_scan_cycle."""
    total = n_kept + n_dropped
    keep_rate = n_kept / total if total else 0.0
    if auc < 0.55:
        return "reject_auc"
    if keep_rate < _META_JUDGE_MIN_KEEP_RATE or threshold >= _META_JUDGE_MAX_THRESHOLD:
        return "reject_degenerate"
    return "deploy"


def test_rejects_the_shipped_degenerate_model():
    # The exact model that was blocking every scan.
    assert _deploy_decision(0.5509, 4, 105, 0.99) == "reject_degenerate"


def test_rejects_low_keep_rate_even_with_ok_auc_and_threshold():
    assert _deploy_decision(0.62, 8, 101, 0.60) == "reject_degenerate"  # 7.3% keep


def test_rejects_near_certain_threshold_even_with_ok_keep_rate():
    assert _deploy_decision(0.62, 40, 69, 0.95) == "reject_degenerate"


def test_still_rejects_noise_auc():
    assert _deploy_decision(0.52, 40, 69, 0.55) == "reject_auc"


def test_deploys_a_healthy_model():
    # Good AUC, keeps ~37% of holdout, sane threshold.
    assert _deploy_decision(0.60, 40, 69, 0.55) == "deploy"


def test_keep_rate_boundary_exactly_at_floor_deploys():
    # Exactly 10% keep-rate is allowed (>= is the deploy side).
    assert _deploy_decision(0.60, 11, 99, 0.55) == "deploy"

"""Deploy gate for the recommendation meta-label model.

The GBM used to be applied whenever a pickle existed on disk — no evidence
required. It now serves predictions only when its chronological holdout
proved positive expectancy lift (mirroring meta_judge's AUC gate).
"""
from __future__ import annotations

from app.services.ml_meta_label import (
    _DEPLOY_MIN_HOLDOUT_N,
    _passes_deploy_gate,
)


def test_should_fail_closed_when_no_holdout_evidence():
    ok, reason = _passes_deploy_gate(None)
    assert ok is False
    assert "fail-closed" in reason
    ok, _ = _passes_deploy_gate({})
    assert ok is False


def test_should_reject_when_holdout_sample_too_small():
    ok, reason = _passes_deploy_gate({
        "holdout_n": _DEPLOY_MIN_HOLDOUT_N - 1,
        "holdout_expectancy_lift": 0.5,
    })
    assert ok is False
    assert "holdout_n" in reason


def test_should_reject_when_filter_adds_no_expectancy():
    for lift in (0.0, -0.2, None):
        ok, _ = _passes_deploy_gate({
            "holdout_n": 100,
            "holdout_expectancy_lift": lift,
        })
        assert ok is False, f"lift={lift} must not deploy"


def test_should_deploy_when_holdout_proves_positive_lift():
    ok, reason = _passes_deploy_gate({
        "holdout_n": 100,
        "holdout_expectancy_lift": 0.12,
    })
    assert ok is True
    assert "expectancy_lift" in reason

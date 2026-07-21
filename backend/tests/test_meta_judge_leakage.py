from __future__ import annotations
"""Regression tests for the two leakage/calibration fixes in meta_judge.

1. Cohort target-encoding is leave-one-out (LOO): a trade's own P&L must not
   appear in its own cohort statistic. Before the fix, a singleton bucket
   handed the model a feature that was a deterministic function of that row's
   own label.
2. Platt scaling + operating threshold are fit on a held-out chronological
   slice (not in-sample) once enough data exists, and the provenance is
   reported honestly in train_meta.
"""
import pytest

from app.services import meta_judge
from app.services.meta_judge import (
    MetaJudge,
    _enrich_with_cohort_stats,
)


# ── LOO target encoding ─────────────────────────────────────────────────────


def test_loo_excludes_own_pnl_in_singleton_bucket():
    """A (symbol, signal_type, dir) bucket with one member must NOT encode
    that member's own P&L — it falls back to the global mean instead."""
    trades = [
        {"symbol": "AAA", "signal_type": "gap_up", "direction": "bullish",
         "regime": "trend_up", "pnl": 5.0},   # unique symcombo (win)
        {"symbol": "BBB", "signal_type": "gap_up", "direction": "bullish",
         "regime": "trend_up", "pnl": -3.0},
        {"symbol": "CCC", "signal_type": "gap_up", "direction": "bullish",
         "regime": "trend_up", "pnl": -3.0},
    ]
    enriched = _enrich_with_cohort_stats(trades)
    a = enriched[0]
    # Own bucket had exactly 1 trade → no other members → global fallback,
    # NOT the row's own 100% WR / +5.0 avg.
    assert a["cohort_symcombo_n"] == 0
    assert a["cohort_symcombo_wr"] != 100.0
    assert a["cohort_symcombo_avg"] != 5.0
    # Global mean over all three: 1 win / 3 = 33.3%, avg = (5-3-3)/3 = -0.33.
    assert a["cohort_symcombo_wr"] == pytest.approx(100.0 / 3.0)
    assert a["cohort_symcombo_avg"] == pytest.approx(-1.0 / 3.0)


def test_loo_combo_stat_removes_self():
    """In a multi-member bucket, the LOO stat equals the average of the OTHER
    members, never including self."""
    trades = [
        {"symbol": "S1", "signal_type": "macd_divergence", "direction": "bullish",
         "regime": "r", "pnl": 10.0},
        {"symbol": "S2", "signal_type": "macd_divergence", "direction": "bullish",
         "regime": "r", "pnl": 2.0},
        {"symbol": "S3", "signal_type": "macd_divergence", "direction": "bullish",
         "regime": "r", "pnl": 4.0},
    ]
    enriched = _enrich_with_cohort_stats(trades)
    # First trade's combo avg excludes its own 10.0 → mean(2,4) = 3.0.
    assert enriched[0]["cohort_combo_avg"] == pytest.approx(3.0)
    assert enriched[0]["cohort_combo_n"] == 2
    # WR excluding self: both others are wins → 100%.
    assert enriched[0]["cohort_combo_wr"] == pytest.approx(100.0)


# ── Held-out Platt calibration ──────────────────────────────────────────────


def _chrono_trades(n: int):
    """Chronological, separable trades alternating win/loss."""
    out = []
    for i in range(n):
        win = i % 2 == 0
        out.append({
            "momentum": 1.0 if win else -1.0,
            "trend": 1.0 if win else -1.0,
            "rsi": 60.0 if win else 40.0,
            "signal_type": "macd_divergence" if win else "double_top",
            "direction": "bullish" if win else "bearish",
            "regime": "trend_up" if win else "trend_down",
            "symbol": "AAA" if win else "BBB",
            "win": win, "pnl": 2.0 if win else -2.0,
        })
    return out


def test_calibration_is_held_out_with_enough_data():
    model = MetaJudge.train(_chrono_trades(120), n_stumps=10, label_mode="win", enrich=False)
    tm = model.train_meta
    assert tm["platt_calibration"] == "held_out"
    # Fit slice is strictly smaller than the full training set.
    assert tm["n_fit"] < tm["n_train"]
    assert tm["n_calibration"] >= meta_judge._MIN_CALIB_SLICE
    assert tm["n_fit"] + tm["n_calibration"] == tm["n_train"]


def test_calibration_falls_back_in_sample_when_thin():
    model = MetaJudge.train(_chrono_trades(20), n_stumps=10, label_mode="win", enrich=False)
    tm = model.train_meta
    assert tm["platt_calibration"] == "in_sample"
    assert tm["n_fit"] == tm["n_train"]


def test_held_out_model_still_separates_and_calibrates():
    """The fix must not break basic behaviour: winners still score above
    losers and probabilities stay in (0, 1)."""
    model = MetaJudge.train(_chrono_trades(120), n_stumps=12, label_mode="win", enrich=False)
    p_win = model.predict_proba({
        "momentum": 1.0, "trend": 1.0, "rsi": 60.0,
        "signal_type": "macd_divergence", "direction": "bullish",
        "regime": "trend_up", "symbol": "AAA",
    })
    p_loss = model.predict_proba({
        "momentum": -1.0, "trend": -1.0, "rsi": 40.0,
        "signal_type": "double_top", "direction": "bearish",
        "regime": "trend_down", "symbol": "BBB",
    })
    # Perfectly-separable fixture can saturate the sigmoid to exactly 1.0;
    # what matters is valid range + correct ordering.
    assert 0.0 <= p_loss < p_win <= 1.0

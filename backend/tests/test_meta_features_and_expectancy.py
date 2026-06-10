from __future__ import annotations
"""Tests for (1) expectancy-maximising threshold calibration in meta_judge and
(2) the uniform feature block stamped onto signals by signal_engine."""
from app.services.meta_judge import _calibrate_threshold
from app.services.signal_engine import attach_meta_features, META_FEATURE_KEYS


def test_expectancy_threshold_prefers_high_pnl_subset():
    # 100 trades: high prob → high pnl, low prob → negative pnl. The expectancy
    # objective must pick a cutoff that keeps the profitable head, not the
    # Youden-J point that maximises raw accuracy.
    probs = [i / 100.0 for i in range(100)]
    pnls = [(-2.0 if p < 0.6 else 3.0) for p in probs]
    labels = [1 if pnl > 0 else 0 for pnl in pnls]
    t = _calibrate_threshold(probs, labels, 0.70, pnls=pnls)
    # Keeping only trades with prob >= t should have positive mean pnl.
    kept = [pnl for p, pnl in zip(probs, pnls) if p >= t]
    assert kept, "threshold kept nothing"
    assert sum(kept) / len(kept) > 0, "kept subset is not profitable"


def test_expectancy_threshold_falls_back_to_youden_without_pnls():
    probs = [0.9, 0.8, 0.4, 0.3, 0.2]
    labels = [1, 1, 0, 0, 0]
    t = _calibrate_threshold(probs, labels, 0.70)  # no pnls → legacy path
    assert 0.0 <= t <= 1.0


def test_attach_meta_features_stamps_top_level_and_metadata():
    sig = {"symbol": "TCS", "signal_type": "rsi_extreme", "direction": "bullish",
           "strength": 7, "metadata": {"rsi": 72}}
    technicals = {
        "current_price": 110.0, "rsi": 72.0, "adx": 28.0, "atr_pct": 1.4,
        "moving_averages": {"sma20": 100.0, "sma50": 95.0, "sma200": 90.0},
    }
    attach_meta_features(sig, technicals=technicals, regime="trend_up",
                         delivery_pct=58.0, vix=13.2, sector="IT")
    # Top-level numeric keys present for the model to split on.
    assert sig["rsi"] == 72.0 and sig["adx"] == 28.0 and sig["atr_pct"] == 1.4
    assert sig["dist_sma200_pct"] == round((110 - 90) / 90 * 100, 3)
    assert sig["regime"] == "trend_up" and sig["sector"] == "IT"
    assert sig["delivery_pct"] == 58.0 and sig["vix"] == 13.2
    # Persisted under metadata for the DB round-trip.
    mf = sig["metadata"]["meta_features"]
    assert mf["rsi"] == 72.0 and mf["regime"] == "trend_up"
    for k in META_FEATURE_KEYS:
        assert k in sig  # all numeric keys stamped


def test_attach_meta_features_is_defensive_on_empty_inputs():
    sig = {"symbol": "X", "signal_type": "breakout", "direction": "bullish"}
    # No technicals, no extras — must not raise, must not invent values.
    attach_meta_features(sig, technicals=None)
    assert "rsi" not in sig  # nothing stamped when unavailable
    assert sig["metadata"]["meta_features"] == {}

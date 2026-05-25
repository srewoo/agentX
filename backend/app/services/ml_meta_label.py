from __future__ import annotations
"""Meta-labeling secondary classifier (López de Prado, AFML Ch. 3).

The primary signal (multi-factor recommendation) decides *direction*;
this module trains a secondary model that predicts *whether* the
primary signal will hit its target before its stop. The secondary
model's output is a probability that gates / sizes the trade:

    final_size = primary_size × p_meta

Empirically improves precision (win rate) by 5–15pp on break-even
factor systems at the cost of recall — exactly what we need to push
the 5d Wilson LB past 50%.

Stack: scikit-learn GradientBoostingClassifier when available
(faithful to AFML's recipe); pure-Python logistic regression fallback
when sklearn is missing. Storage: model pickled to
`<DB dir>/meta_label.pkl`; train data sourced from
`recommendation_outcomes` once ≥200 resolved trades exist.
"""
import json
import logging
import math
import pickle
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from app.database import DB_PATH

logger = logging.getLogger(__name__)

_MIN_TRAIN_SAMPLES = 200
_MODEL_PATH = Path(DB_PATH).parent / "meta_label.pkl"

# Feature names — must match the order in `_features_from_signals`.
FEATURE_NAMES = [
    "weighted_score", "factor_agreement", "risk_reward", "conviction",
    # Per-factor scores (signed by primary direction).
    "f_trend", "f_momentum", "f_volume_delivery", "f_fno_oi", "f_fii_dii",
    "f_rel_strength", "f_news_sentiment", "f_volatility", "f_fundamentals",
    "f_weekly_trend",
    # Contextual.
    "is_buy", "regime_trend_up", "regime_trend_down", "regime_risk_off",
]

_in_memory_model: Optional[Any] = None


def _features_from_signals(rec: dict[str, Any]) -> list[float]:
    """Build a feature vector from a stored recommendation row."""
    sigs = rec.get("signals_json")
    if isinstance(sigs, str):
        try:
            sigs = json.loads(sigs)
        except Exception:
            sigs = []
    sigs = sigs or []
    sign = 1.0 if rec.get("action") == "BUY" else -1.0
    by_name: dict[str, float] = {
        s.get("name", ""): sign * float(s.get("score") or 0.0) for s in sigs
    }
    regime = rec.get("regime") or ""
    return [
        float(rec.get("weighted_score") or 0.0) * sign,
        float(rec.get("factor_agreement") or 0.0),
        # Risk:reward — derive from entry/sl/target1.
        _rr(rec),
        float(rec.get("conviction") or 0.0) / 100.0,
        by_name.get("trend", 0.0),
        by_name.get("momentum", 0.0),
        by_name.get("volume_delivery", 0.0),
        by_name.get("fno_oi", 0.0),
        by_name.get("fii_dii", 0.0),
        by_name.get("rel_strength", 0.0),
        by_name.get("news_sentiment", 0.0),
        by_name.get("volatility", 0.0),
        by_name.get("fundamentals", 0.0),
        by_name.get("weekly_trend", 0.0),
        1.0 if rec.get("action") == "BUY" else 0.0,
        1.0 if regime == "trend_up" else 0.0,
        1.0 if regime == "trend_down" else 0.0,
        1.0 if regime == "risk_off" else 0.0,
    ]


def _rr(rec: dict[str, Any]) -> float:
    try:
        e = float(rec["entry"]); s = float(rec["stoploss"]); t = float(rec["target1"])
    except Exception:
        return 0.0
    risk = abs(e - s)
    if risk <= 0:
        return 0.0
    return min(5.0, abs(t - e) / risk)


async def _load_training_data() -> tuple[list[list[float]], list[int], list[int]]:
    """Pull resolved BUY/SELL outcomes. Returns (X, y, label_horizons)."""
    X: list[list[float]] = []
    y: list[int] = []
    horizons: list[int] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT action, signals_json, weighted_score, factor_agreement,
                      conviction, entry, stoploss, target1, regime,
                      outcome, timeframe_days
               FROM recommendation_outcomes
               WHERE outcome IN ('win','loss')"""
        ) as cur:
            async for r in cur:
                row = dict(r)
                X.append(_features_from_signals(row))
                y.append(1 if row["outcome"] == "win" else 0)
                horizons.append(int(row.get("timeframe_days") or 5))
    return X, y, horizons


def _try_sklearn():
    try:
        from sklearn.ensemble import GradientBoostingClassifier
        return GradientBoostingClassifier
    except Exception:
        return None


def _fit_logistic_fallback(X: list[list[float]], y: list[int]) -> dict[str, Any]:
    """Logistic regression fallback when sklearn isn't installed."""
    from app.services.recommendation_tuner import _logistic_regression
    coefs, acc = _logistic_regression(X, y, epochs=400)
    return {"kind": "logistic", "coefs": coefs, "intercept": 0.0, "accuracy": acc}


def _predict_one(model: Any, x: list[float]) -> float:
    """Return p(win)."""
    if model is None:
        return 0.5
    if isinstance(model, dict) and model.get("kind") == "logistic":
        z = model.get("intercept", 0.0) + sum(w * v for w, v in zip(model["coefs"], x))
        z = max(-30.0, min(30.0, z))
        return 1.0 / (1.0 + math.exp(-z))
    # sklearn estimator path.
    try:
        proba = model.predict_proba([x])[0]
        # Classes are sorted ascending: [P(0), P(1)].
        return float(proba[1])
    except Exception:
        return 0.5


async def train_meta_label_model(n_splits: int = 5) -> dict[str, Any]:
    """Fit + persist the meta-labeling classifier.

    Uses purged K-fold CV to estimate OOS accuracy without label leakage.
    Final model is fit on the full dataset (standard practice — CV is for
    *estimation*; deployment uses all data).
    """
    from app.services.ml_labeling import purged_kfold_split
    X, y, horizons = await _load_training_data()
    if len(X) < _MIN_TRAIN_SAMPLES:
        return {"status": "insufficient_data", "samples": len(X), "required": _MIN_TRAIN_SAMPLES}

    import numpy as np
    X_arr = np.array(X)
    y_arr = np.array(y)

    Cls = _try_sklearn()
    cv_scores: list[float] = []
    for train_idx, test_idx in purged_kfold_split(len(X), horizons, n_splits=n_splits):
        if Cls is not None:
            clf = Cls(n_estimators=200, max_depth=3, learning_rate=0.05, random_state=42)
            clf.fit(X_arr[train_idx], y_arr[train_idx])
            preds = clf.predict(X_arr[test_idx])
            acc = float((preds == y_arr[test_idx]).mean())
        else:
            m = _fit_logistic_fallback(X_arr[train_idx].tolist(), y_arr[train_idx].tolist())
            correct = sum(
                1 for x, t in zip(X_arr[test_idx], y_arr[test_idx])
                if (_predict_one(m, list(x)) >= 0.5) == bool(t)
            )
            acc = correct / max(1, len(test_idx))
        cv_scores.append(acc)

    # Final fit on all data.
    if Cls is not None:
        final = Cls(n_estimators=200, max_depth=3, learning_rate=0.05, random_state=42)
        final.fit(X_arr, y_arr)
    else:
        final = _fit_logistic_fallback(X, y)

    _MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_MODEL_PATH, "wb") as f:
        pickle.dump({
            "model": final,
            "feature_names": FEATURE_NAMES,
            "n_train": len(X),
            "cv_accuracy_mean": sum(cv_scores) / max(1, len(cv_scores)),
            "cv_accuracy_folds": cv_scores,
            "kind": "sklearn_gbm" if Cls is not None else "logistic_fallback",
        }, f)

    global _in_memory_model
    _in_memory_model = final
    return {
        "status": "trained",
        "samples": len(X),
        "cv_accuracy_mean": round(sum(cv_scores) / max(1, len(cv_scores)), 4),
        "cv_accuracy_folds": [round(v, 4) for v in cv_scores],
        "kind": "sklearn_gbm" if Cls is not None else "logistic_fallback",
        "model_path": str(_MODEL_PATH),
    }


def load_meta_label_model() -> Optional[Any]:
    """Load the persisted model, or return None if not yet trained."""
    global _in_memory_model
    if _in_memory_model is not None:
        return _in_memory_model
    if not _MODEL_PATH.exists():
        return None
    try:
        with open(_MODEL_PATH, "rb") as f:
            bundle = pickle.load(f)
        _in_memory_model = bundle.get("model")
        return _in_memory_model
    except Exception as e:
        logger.debug("meta-label model load failed: %s", e)
        return None


def meta_label_probability(rec_payload: dict[str, Any]) -> Optional[float]:
    """Return p(win) for an in-memory recommendation, or None when no
    model is available (caller falls back to primary signal alone).

    `rec_payload` matches the dict shape stored in
    `recommendation_outcomes` (action, signals_json or signals list,
    weighted_score, factor_agreement, conviction, entry, stoploss,
    target1, regime).
    """
    model = load_meta_label_model()
    if model is None:
        return None
    # Allow both stored-row shape (signals_json) and live shape (signals list).
    if "signals_json" not in rec_payload and "signals" in rec_payload:
        try:
            rec_payload = {
                **rec_payload,
                "signals_json": json.dumps([
                    {"name": getattr(s, "name", None) or s.get("name"),
                     "score": float(getattr(s, "score", None) if hasattr(s, "score") else s.get("score") or 0.0)}
                    for s in rec_payload["signals"]
                ]),
            }
        except Exception:
            pass
    x = _features_from_signals(rec_payload)
    return _predict_one(model, x)

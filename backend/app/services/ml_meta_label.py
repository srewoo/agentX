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


async def _load_training_data() -> tuple[list[list[float]], list[int], list[int], list[float]]:
    """Pull resolved BUY/SELL outcomes in chronological order.

    Returns ``(X, y, label_horizons, pnl_pct)``. Rows are ordered by
    ``created_at`` so a *chronological* holdout (train on the past, test on
    the future) is meaningful — a random split would leak future regime
    information into the training fold and overstate live accuracy. ``pnl_pct``
    is carried alongside so the holdout can be scored on realized expectancy,
    not just classification accuracy.
    """
    X: list[list[float]] = []
    y: list[int] = []
    horizons: list[int] = []
    pnl: list[float] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT action, signals_json, weighted_score, factor_agreement,
                      conviction, entry, stoploss, target1, regime,
                      outcome, timeframe_days, pnl_pct
               FROM recommendation_outcomes
               WHERE outcome IN ('win','loss')
               ORDER BY created_at ASC"""
        ) as cur:
            async for r in cur:
                row = dict(r)
                X.append(_features_from_signals(row))
                y.append(1 if row["outcome"] == "win" else 0)
                horizons.append(int(row.get("timeframe_days") or 5))
                pnl.append(float(row.get("pnl_pct") or 0.0))
    return X, y, horizons, pnl


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


# Caveat surfaced in every train result. The model is trained on
# `recommendation_outcomes` — the trades the *primary* engine chose to
# recommend, not a random sample of all candidate signal-bars. It therefore
# learns "was the primary engine right?" and can inherit the primary's
# selection bias rather than fully correcting it. Treat its probability as a
# *re-ranking* signal on already-surfaced candidates, not an unbiased estimate
# of any trade's win rate. The honest performance number is `holdout_*` below
# (chronological out-of-sample), not the in-sample CV mean.
_SELECTION_BIAS_CAVEAT = (
    "Trained on engine-selected recommendations, not a random candidate "
    "universe; p is a re-ranking signal, not an unbiased win-rate. Judge it "
    "by holdout_accuracy/holdout_expectancy, not cv_accuracy_mean."
)

# Fraction of the (chronological) tail held out as a true out-of-sample test.
_HOLDOUT_FRACTION = 0.2


def _fit(Cls, X_arr, y_arr):
    """Fit a fresh classifier (sklearn GBM if available, else logistic)."""
    if Cls is not None:
        clf = Cls(n_estimators=200, max_depth=3, learning_rate=0.05, random_state=42)
        clf.fit(X_arr, y_arr)
        return clf
    return _fit_logistic_fallback(X_arr.tolist(), y_arr.tolist())


async def train_meta_label_model(
    n_splits: int = 5,
    *,
    recent_window: int | None = None,
) -> dict[str, Any]:
    """Fit + persist the meta-labeling classifier.

    Uses purged K-fold CV to *estimate* OOS accuracy without label leakage,
    then evaluates a **chronological holdout** (last ``_HOLDOUT_FRACTION`` of
    trades by time) as the honest live-performance proxy — both classification
    accuracy and realized expectancy of the trades the model would keep. The
    deployed model is fit on the training portion + holdout (CV/holdout are for
    estimation), but see ``recent_window``.

    ``recent_window``: when set, the *deployed* model is fit only on the most
    recent N resolved trades. The signal engine drifts across regimes; training
    on the whole history can anchor the meta-label to stale regimes. Pass e.g.
    1000 to keep the model on recent behavior. ``None`` keeps the all-data fit.

    The returned dict always carries ``selection_bias`` — read it before
    trusting the probability (see ``_SELECTION_BIAS_CAVEAT``).
    """
    from app.services.ml_labeling import purged_kfold_split
    X, y, horizons, pnl = await _load_training_data()
    if len(X) < _MIN_TRAIN_SAMPLES:
        return {
            "status": "insufficient_data",
            "samples": len(X),
            "required": _MIN_TRAIN_SAMPLES,
            "selection_bias": _SELECTION_BIAS_CAVEAT,
        }

    import numpy as np
    X_arr = np.array(X)
    y_arr = np.array(y)
    pnl_arr = np.array(pnl)

    Cls = _try_sklearn()
    cv_scores: list[float] = []
    for train_idx, test_idx in purged_kfold_split(len(X), horizons, n_splits=n_splits):
        m = _fit(Cls, X_arr[train_idx], y_arr[train_idx])
        if Cls is not None:
            acc = float((m.predict(X_arr[test_idx]) == y_arr[test_idx]).mean())
        else:
            correct = sum(
                1 for x, t in zip(X_arr[test_idx], y_arr[test_idx])
                if (_predict_one(m, list(x)) >= 0.5) == bool(t)
            )
            acc = correct / max(1, len(test_idx))
        cv_scores.append(acc)

    # ── Chronological holdout: train on the past, test on the future. ──
    # This is the number that actually predicts live behavior; CV can still
    # leak regime structure across folds.
    split = int(len(X) * (1.0 - _HOLDOUT_FRACTION))
    holdout: dict[str, Any] = {}
    if split >= _MIN_TRAIN_SAMPLES // 2 and split < len(X):
        hm = _fit(Cls, X_arr[:split], y_arr[:split])
        probs = np.array([_predict_one(hm, list(x)) for x in X_arr[split:]])
        truth = y_arr[split:]
        kept = probs >= 0.5
        base_exp = float(pnl_arr[split:].mean())
        kept_exp = float(pnl_arr[split:][kept].mean()) if kept.any() else 0.0
        holdout = {
            "holdout_n": int(len(truth)),
            "holdout_accuracy": round(float(((probs >= 0.5) == truth).mean()), 4),
            "holdout_base_rate": round(float(truth.mean()), 4),
            # The filter's value: expectancy of kept trades vs all trades.
            "holdout_expectancy_kept": round(kept_exp, 4),
            "holdout_expectancy_all": round(base_exp, 4),
            "holdout_expectancy_lift": round(kept_exp - base_exp, 4),
            "holdout_kept_fraction": round(float(kept.mean()), 4),
        }

    # ── Deployed model. ──
    if recent_window is not None and recent_window > 0 and recent_window < len(X):
        fit_X, fit_y = X_arr[-recent_window:], y_arr[-recent_window:]
        trained_on = f"recent_{recent_window}"
    else:
        fit_X, fit_y = X_arr, y_arr
        trained_on = "all"
    final = _fit(Cls, fit_X, fit_y)

    _MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_MODEL_PATH, "wb") as f:
        pickle.dump({
            "model": final,
            "feature_names": FEATURE_NAMES,
            "n_train": int(len(fit_X)),
            "trained_on": trained_on,
            "cv_accuracy_mean": sum(cv_scores) / max(1, len(cv_scores)),
            "cv_accuracy_folds": cv_scores,
            "holdout": holdout,
            "kind": "sklearn_gbm" if Cls is not None else "logistic_fallback",
        }, f)

    global _in_memory_model
    _in_memory_model = final
    return {
        "status": "trained",
        "samples": len(X),
        "trained_on": trained_on,
        "cv_accuracy_mean": round(sum(cv_scores) / max(1, len(cv_scores)), 4),
        "cv_accuracy_folds": [round(v, 4) for v in cv_scores],
        **holdout,
        "kind": "sklearn_gbm" if Cls is not None else "logistic_fallback",
        "model_path": str(_MODEL_PATH),
        "selection_bias": _SELECTION_BIAS_CAVEAT,
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

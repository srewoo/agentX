from __future__ import annotations
"""2.4 — Fit the conviction magic numbers, or keep the hand-tuned stack.

`recommendation._calibrate_conviction` multiplies a stack of hand-picked
constants: ``0.75 + 0.35·agreement``, ``×0.85`` / ``×1.05`` on risk:reward,
``×0.90`` / ``×1.05`` on regime. Each constant is an untested guess. This module
replaces that stack with ONE logistic model over the SAME factors
(score magnitude, factor agreement, risk:reward, regime) and only lets it serve
if it BEATS the hand-tuned stack on a chronological holdout — otherwise the
multiplicative stack stays. "Any constant that can't earn its place in a
regression is noise."

Honesty rails (mirroring ml_meta_label):
  * **Chronological holdout** (train past → test future), never in-sample CV.
  * **Beat-the-baseline deploy gate.** Deploy only when, on the holdout, ranking
    trades by the model's p(win) yields higher realised expectancy than the
    existing multiplicative conviction — on the SAME number of kept trades.
    Fail-closed: too little evidence, or no lift ⇒ not deployed.
  * **Scale-preserving output.** The model's p is mapped onto the baseline
    conviction scale (moment-matching) so downstream conviction thresholds keep
    their meaning; the model changes the *ordering*, not the goalposts.

The fit and the gate are pure functions (no DB) so they unit-test cleanly.
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

FEATURE_NAMES = ["score_mag", "agreement", "risk_reward", "regime_trend", "regime_risk_off"]

_MODEL_PATH = Path(__file__).resolve().parent.parent.parent / "models" / "conviction_model.pkl"
_MIN_TRAIN_SAMPLES = 120
_HOLDOUT_FRACTION = 0.2
_DEPLOY_MIN_HOLDOUT_N = 40
_DEPLOY_MIN_EXPECTANCY_LIFT = 0.0   # model must strictly beat the hand-tuned stack
_BASELINE_MIN_CONVICTION = 65       # the live conviction floor the baseline keeps at

_in_memory: Optional[dict[str, Any]] = None
_gate_rejected = False


# ── feature extraction (pure) ────────────────────────────────
def risk_reward(entry: float, stop: float, target: float) -> float:
    """Reward:risk ratio; 0.0 when the risk leg is degenerate."""
    risk = abs(entry - stop)
    reward = abs(target - entry)
    return round(reward / risk, 4) if risk > 0 else 0.0


def extract_features(
    *, weighted_score: float, agreement: float, risk_reward: float,
    regime: Optional[str],
) -> list[float]:
    """The SAME factors the multiplicative stack consumes, as a feature vector."""
    reg = (regime or "").lower()
    return [
        abs(float(weighted_score or 0.0)),
        float(agreement or 0.0),
        min(float(risk_reward or 0.0), 5.0),            # clamp fat tails
        1.0 if reg in ("trend_up", "trend_down") else 0.0,
        1.0 if reg == "risk_off" else 0.0,
    ]


# ── logistic fit (pure, standardized) ────────────────────────
def _standardize(X: list[list[float]]) -> tuple[list[list[float]], list[float], list[float]]:
    n_feat = len(X[0])
    means = [sum(row[j] for row in X) / len(X) for j in range(n_feat)]
    stds = []
    for j in range(n_feat):
        var = sum((row[j] - means[j]) ** 2 for row in X) / max(1, len(X))
        stds.append(math.sqrt(var) or 1.0)
    Z = [[(row[j] - means[j]) / stds[j] for j in range(n_feat)] for row in X]
    return Z, means, stds


def fit_logistic(
    X: list[list[float]], y: list[int], *, lr: float = 0.1, epochs: int = 500, l2: float = 0.01,
) -> dict[str, Any]:
    """Standardized batch logistic regression with intercept. Returns a bundle
    ``{coefs, intercept, means, stds}`` usable by :func:`predict_p`."""
    if not X:
        return {"coefs": [0.0] * len(FEATURE_NAMES), "intercept": 0.0,
                "means": [0.0] * len(FEATURE_NAMES), "stds": [1.0] * len(FEATURE_NAMES)}
    Z, means, stds = _standardize(X)
    n_feat = len(Z[0])
    w = [0.0] * n_feat
    b = 0.0
    n = len(Z)
    for _ in range(epochs):
        gw = [0.0] * n_feat
        gb = 0.0
        for zi, yi in zip(Z, y):
            z = b + sum(wj * v for wj, v in zip(w, zi))
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
            err = p - yi
            gb += err
            for j in range(n_feat):
                gw[j] += err * zi[j]
        b -= lr * (gb / n)
        for j in range(n_feat):
            w[j] = (1 - lr * l2) * w[j] - lr * (gw[j] / n)
    return {"coefs": w, "intercept": b, "means": means, "stds": stds}


def predict_p(bundle: dict[str, Any], x: list[float]) -> float:
    """p(win) for a raw (un-standardized) feature vector."""
    means, stds = bundle["means"], bundle["stds"]
    z = bundle["intercept"] + sum(
        wj * ((xj - m) / s) for wj, xj, m, s in zip(bundle["coefs"], x, means, stds)
    )
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


# ── beat-the-baseline deploy gate (pure) ─────────────────────
def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def evaluate_holdout(
    model_p: list[float], baseline_conviction: list[float], pnl: list[float],
    *, keep_conviction: int = _BASELINE_MIN_CONVICTION,
) -> dict[str, Any]:
    """Compare model ranking vs the hand-tuned conviction on a holdout.

    The baseline keeps trades with ``conviction ≥ keep_conviction``; the model is
    allowed to keep the SAME NUMBER of trades, chosen by highest p(win). Lift is
    the difference in realised expectancy of the kept sets. Positive ⇒ the fitted
    model orders trades better than the constant stack.
    """
    n = len(pnl)
    k = sum(1 for c in baseline_conviction if c >= keep_conviction)
    base_kept = [pnl[i] for i in range(n) if baseline_conviction[i] >= keep_conviction]
    # Model keeps its top-k by p(win).
    order = sorted(range(n), key=lambda i: model_p[i], reverse=True)[:k]
    model_kept = [pnl[i] for i in order]
    base_exp = _mean(base_kept)
    model_exp = _mean(model_kept)
    return {
        "holdout_n": n,
        "kept_n": k,
        "baseline_expectancy": round(base_exp, 4),
        "model_expectancy": round(model_exp, 4),
        "expectancy_lift": round(model_exp - base_exp, 4),
    }


def passes_deploy_gate(holdout: Optional[dict[str, Any]]) -> tuple[bool, str]:
    if not holdout:
        return False, "no holdout evidence (fail-closed)"
    n = int(holdout.get("holdout_n") or 0)
    kept = int(holdout.get("kept_n") or 0)
    lift = holdout.get("expectancy_lift")
    if n < _DEPLOY_MIN_HOLDOUT_N or kept < 5:
        return False, f"holdout too small (n={n}, kept={kept})"
    if lift is None or float(lift) <= _DEPLOY_MIN_EXPECTANCY_LIFT:
        return False, f"expectancy_lift={lift} — model does not beat the hand-tuned stack"
    return True, f"model beats baseline on holdout: lift=+{float(lift):.3f} (n={n}, kept={kept})"


# ── scale-preserving p → conviction map ──────────────────────
def fit_conviction_map(model_p: list[float], baseline_conviction: list[float]) -> dict[str, float]:
    """Moment-match model p onto the baseline conviction scale.

    conviction = mean_conv + (p − mean_p)/std_p · std_conv. Preserves the model's
    ordering while keeping the numeric scale (and the ≥65 floor) meaningful.
    """
    mp = _mean(model_p)
    sp = math.sqrt(_mean([(p - mp) ** 2 for p in model_p])) or 1.0
    mc = _mean(baseline_conviction)
    sc = math.sqrt(_mean([(c - mc) ** 2 for c in baseline_conviction])) or 1.0
    return {"mean_p": mp, "std_p": sp, "mean_conv": mc, "std_conv": sc}


def map_to_conviction(p: float, cmap: dict[str, float]) -> int:
    conv = cmap["mean_conv"] + (p - cmap["mean_p"]) / cmap["std_p"] * cmap["std_conv"]
    return max(0, min(100, int(round(conv))))


# ── training / persistence (DB) ──────────────────────────────
async def _load_training_data() -> tuple[list[list[float]], list[int], list[float], list[float]]:
    """(features, win-labels, baseline_conviction, pnl) over RESOLVED recos,
    chronological by created_at. Honors the pinned holdout: selection training
    never reads the reserved window."""
    from app.services import holdout as _holdout

    boundary = await _holdout.resolve_boundary()
    X: list[list[float]] = []
    y: list[int] = []
    conv: list[float] = []
    pnl: list[float] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT weighted_score, factor_agreement, entry, stoploss, target1, "
            "regime, conviction, outcome, pnl_pct, created_at "
            "FROM recommendation_outcomes "
            "WHERE outcome IN ('win','loss') AND weighted_score IS NOT NULL "
            "ORDER BY created_at ASC"
        ) as cur:
            for r in await cur.fetchall():
                if boundary is not None and r["created_at"]:
                    try:
                        from datetime import datetime
                        if datetime.fromisoformat(str(r["created_at"])).date() > boundary:
                            continue  # reserved holdout — never train selection on it
                    except (ValueError, TypeError):
                        pass
                X.append(extract_features(
                    weighted_score=r["weighted_score"], agreement=r["factor_agreement"] or 0.0,
                    risk_reward=risk_reward(r["entry"], r["stoploss"], r["target1"]),
                    regime=r["regime"]))
                y.append(1 if r["outcome"] == "win" else 0)
                conv.append(float(r["conviction"] or 0))
                pnl.append(float(r["pnl_pct"] or 0.0))
    return X, y, conv, pnl


async def train_conviction_model() -> dict[str, Any]:
    """Fit the conviction model, gate it against the hand-tuned stack, persist."""
    X, y, conv, pnl = await _load_training_data()
    if len(X) < _MIN_TRAIN_SAMPLES:
        return {"status": "insufficient_data", "samples": len(X), "required": _MIN_TRAIN_SAMPLES}

    split = int(len(X) * (1.0 - _HOLDOUT_FRACTION))
    holdout: dict[str, Any] = {}
    if split >= _MIN_TRAIN_SAMPLES // 2 and split < len(X):
        hm = fit_logistic(X[:split], y[:split])
        hp = [predict_p(hm, x) for x in X[split:]]
        holdout = evaluate_holdout(hp, conv[split:], pnl[split:])

    final = fit_logistic(X, y)
    cmap = fit_conviction_map([predict_p(final, x) for x in X], conv)
    deployed, reason = passes_deploy_gate(holdout)

    _MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    bundle = {"model": final, "conviction_map": cmap, "holdout": holdout,
              "feature_names": FEATURE_NAMES, "n_train": len(X)}
    with open(_MODEL_PATH, "wb") as f:
        pickle.dump(bundle, f)

    global _in_memory, _gate_rejected
    if deployed:
        _in_memory, _gate_rejected = bundle, False
    else:
        _in_memory, _gate_rejected = None, True
        logger.warning("conviction model trained but NOT deployed: %s", reason)
    return {"status": "trained", "deployed": deployed, "deploy_gate": reason,
            "samples": len(X), **holdout, "model_path": str(_MODEL_PATH)}


def _load_bundle() -> Optional[dict[str, Any]]:
    global _in_memory, _gate_rejected
    if _in_memory is not None:
        return _in_memory
    if _gate_rejected or not _MODEL_PATH.exists():
        return None
    try:
        with open(_MODEL_PATH, "rb") as f:
            bundle = pickle.load(f)
        ok, reason = passes_deploy_gate(bundle.get("holdout"))
        if not ok:
            logger.warning("conviction model on disk NOT deployed: %s", reason)
            _gate_rejected = True
            return None
        _in_memory = bundle
        return bundle
    except Exception as e:
        logger.debug("conviction model load failed: %s", e)
        return None


def model_conviction(
    *, weighted_score: float, agreement: float, risk_reward: float,
    regime: Optional[str],
) -> Optional[int]:
    """Fitted conviction on the baseline scale, or None when no model is
    deployed (caller falls back to the multiplicative stack)."""
    bundle = _load_bundle()
    if bundle is None:
        return None
    x = extract_features(weighted_score=weighted_score, agreement=agreement,
                         risk_reward=risk_reward, regime=regime)
    return map_to_conviction(predict_p(bundle["model"], x), bundle["conviction_map"])


def _reset_cache() -> None:
    """Test hook."""
    global _in_memory, _gate_rejected
    _in_memory, _gate_rejected = None, False

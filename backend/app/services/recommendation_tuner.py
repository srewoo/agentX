from __future__ import annotations
"""Offline factor-weight tuner.

Replaces hand-picked priors (trend=16%, momentum=12%, ...) with weights
learned from realised PnL stored in `recommendation_outcomes`. Two modes:

  1. `logistic_fit_weights()` — fits a logistic regression (win vs loss)
     on per-factor scores and converts learned coefficients into a
     normalised weight vector. Closed-loop with the existing tracker:
     once 200+ resolved trades exist, learned weights take over.

  2. `grid_search_weights()` — coarser fallback that perturbs each
     factor's prior ±50% and picks the combination with highest pooled
     OOS Sharpe on the walk-forward universe runner. Slow but doesn't
     need labelled outcomes — useful for cold-start.

Both write the result to a `factor_weights` table and update an
in-memory cache that `recommendation._select_weights` reads before
falling back to `WEIGHTS_CALM` / `WEIGHTS_RISK_OFF`.
"""
import asyncio
import json
import logging
import math
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite

from app.database import DB_PATH

logger = logging.getLogger(__name__)

# Same factor names as recommendation_factors.WEIGHTS_CALM.
_FACTORS = [
    "trend", "momentum", "volume_delivery", "fno_oi", "fii_dii",
    "rel_strength", "news_sentiment", "volatility", "fundamentals",
    "weekly_trend",
]

# Cache key: regime ("calm" / "risk_off"). Empty cache = use module priors.
_learned_weights: dict[str, dict[str, float]] = {}

_MIN_FIT_SAMPLES = 200  # below this we don't trust the fit


async def _ensure_weights_table() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS factor_weights (
                regime TEXT PRIMARY KEY,
                weights_json TEXT NOT NULL,
                n_samples INTEGER NOT NULL,
                fit_method TEXT NOT NULL,
                accuracy REAL,
                updated_at TEXT NOT NULL
            )"""
        )
        await db.commit()


async def _load_resolved_dataset(regime: Optional[str] = None) -> list[dict[str, Any]]:
    """Return [{factors: {name: score}, win: 0/1, pnl: float, regime: str}, ...]."""
    query = """SELECT signals_json, action, outcome, pnl_pct, regime
               FROM recommendation_outcomes
               WHERE outcome IN ('win','loss')
                 AND signals_json IS NOT NULL"""
    params: tuple = ()
    if regime:
        query += " AND regime = ?"
        params = (regime,)

    rows: list[dict[str, Any]] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            async for r in cur:
                try:
                    sigs = json.loads(r["signals_json"] or "[]")
                except Exception:
                    continue
                # Sign-flip scores for SELL so positive == aligned-with-call.
                sign = 1.0 if r["action"] == "BUY" else -1.0
                factors = {s.get("name"): sign * float(s.get("score") or 0.0) for s in sigs}
                rows.append({
                    "factors": factors,
                    "win": 1 if r["outcome"] == "win" else 0,
                    "pnl": float(r["pnl_pct"] or 0.0),
                    "regime": r["regime"] or "neutral",
                })
    return rows


def _logistic_regression(
    X: list[list[float]], y: list[int], *, lr: float = 0.05, epochs: int = 400, l2: float = 0.01,
) -> tuple[list[float], float]:
    """Tiny batch logistic regression with L2. Returns (coefs, accuracy).

    Pure-Python so we don't need scikit-learn at runtime. 10 features × a
    few thousand samples runs in well under a second.
    """
    if not X:
        return [0.0] * len(_FACTORS), 0.0
    n_feat = len(X[0])
    w = [0.0] * n_feat
    b = 0.0
    n = len(X)
    for _ in range(epochs):
        grad_w = [0.0] * n_feat
        grad_b = 0.0
        for xi, yi in zip(X, y):
            z = b + sum(wj * xij for wj, xij in zip(w, xi))
            # Numerically stable sigmoid.
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
            err = p - yi
            grad_b += err
            for j in range(n_feat):
                grad_w[j] += err * xi[j]
        b -= lr * (grad_b / n)
        for j in range(n_feat):
            w[j] = (1 - lr * l2) * w[j] - lr * (grad_w[j] / n)
    # Accuracy
    correct = 0
    for xi, yi in zip(X, y):
        z = b + sum(wj * xij for wj, xij in zip(w, xi))
        pred = 1 if z >= 0 else 0
        if pred == yi:
            correct += 1
    return w, correct / n


def _coefs_to_weights(coefs: list[float]) -> dict[str, float]:
    """Turn logistic coefficients into a normalised weight vector.

    Negative coefficients (factor was anti-predictive when aligned) get
    floored at a small floor so they still contribute marginally — the
    learned-edge runtime multiplier handles fine-grained downweighting.
    """
    floor = 0.01
    raw = [max(floor, c) for c in coefs]
    total = sum(raw)
    if total <= 0:
        return {f: 1.0 / len(_FACTORS) for f in _FACTORS}
    return {f: round(v / total, 4) for f, v in zip(_FACTORS, raw)}


async def logistic_fit_weights(regime: Optional[str] = None) -> dict[str, Any]:
    """Fit factor weights from resolved win/loss outcomes."""
    await _ensure_weights_table()
    dataset = await _load_resolved_dataset(regime=regime)
    if len(dataset) < _MIN_FIT_SAMPLES:
        return {
            "status": "insufficient_data",
            "samples": len(dataset),
            "required": _MIN_FIT_SAMPLES,
        }

    X = [[row["factors"].get(f, 0.0) for f in _FACTORS] for row in dataset]
    y = [row["win"] for row in dataset]
    coefs, acc = _logistic_regression(X, y)
    weights = _coefs_to_weights(coefs)
    regime_key = regime or "all"
    await _persist_weights(regime_key, weights, len(dataset), "logistic", acc)
    _learned_weights[regime_key] = weights
    return {
        "status": "fitted",
        "regime": regime_key,
        "samples": len(dataset),
        "accuracy": round(acc, 4),
        "coefficients": dict(zip(_FACTORS, [round(c, 4) for c in coefs])),
        "weights": weights,
    }


async def _persist_weights(
    regime: str, weights: dict[str, float], n: int, method: str, acc: Optional[float],
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO factor_weights
                 (regime, weights_json, n_samples, fit_method, accuracy, updated_at)
                 VALUES (?, ?, ?, ?, ?, ?)""",
            (regime, json.dumps(weights), n, method, acc, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def seed_learned_weights() -> int:
    """Load persisted weights on startup. Returns count loaded."""
    await _ensure_weights_table()
    loaded = 0
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT regime, weights_json, n_samples FROM factor_weights") as cur:
            async for r in cur:
                try:
                    if (r["n_samples"] or 0) >= _MIN_FIT_SAMPLES:
                        _learned_weights[r["regime"]] = json.loads(r["weights_json"])
                        loaded += 1
                except Exception:
                    continue
    logger.info("Learned factor weights seeded: %d regime profiles", loaded)
    return loaded


def get_learned_weights(regime: Optional[str] = None) -> Optional[dict[str, float]]:
    """Return learned weights for the regime if available, else None.

    Caller (recommendation._select_weights) falls back to hardcoded priors.
    """
    if regime and regime in _learned_weights:
        return _learned_weights[regime]
    return _learned_weights.get("all")


def get_learned_weights_snapshot() -> dict[str, dict[str, float]]:
    """Read-only view of all cached learned weights (for /performance/insights)."""
    return {k: dict(v) for k, v in _learned_weights.items()}

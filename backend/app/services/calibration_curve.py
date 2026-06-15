from __future__ import annotations
"""C1 — conviction calibration curve.

The recommendation engine emits a 0–100 *conviction*. Nothing today proves
that "70" means "wins ~70% of the time" — `calibrated_conviction()` is a
heuristic (agreement/regime/risk nudges), not an empirically-fitted map. This
module closes that gap: it reads resolved `recommendation_outcomes`, measures
the realized win rate at each conviction level, and fits a **monotonic**
conviction→p(win) curve via isotonic regression (Pool Adjacent Violators — no
sklearn dependency). It reports a reliability diagram and a Brier score so the
calibration is *measured*, not asserted.

Design choices:
  * **Isotonic, not Platt.** We only assume higher conviction ⇒ not-lower
    win rate (monotonicity), not a logistic shape. PAV is exact, O(n), and
    dependency-free.
  * **Honest about sample size.** Below `_MIN_SAMPLES` resolved trades we
    refuse to fit and say so — a curve from 30 trades is noise.
  * **Read-only by default.** `build_calibration_curve()` fits + persists the
    curve and its Brier score; it does NOT silently rewire live conviction.
    `apply_curve()` is the accessor a caller opts into (e.g. Kelly sizing),
    so the behaviour change is explicit and reversible.

The Brier score (mean squared error of probability vs 0/1 outcome) is reported
two ways — using the *raw* conviction/100 as the probability, and using the
*calibrated* probability — so "did calibration help?" is a number, not a vibe.
"""
import json
import logging
from typing import Any, Optional

import aiosqlite

from app.database import DB_PATH

logger = logging.getLogger(__name__)

_SETTINGS_KEY = "recommendation_calibration_curve"
_MIN_SAMPLES = 100  # below this, a fitted curve is noise — refuse to fit


def isotonic_fit(xs: list[float], ys: list[float]) -> list[tuple[float, float]]:
    """Monotone-increasing fit of ys on xs via Pool Adjacent Violators.

    Returns a list of (x, fitted_y) breakpoints, sorted by x, with fitted_y
    non-decreasing. ``xs`` need not be sorted or unique. ``ys`` are 0/1 (or
    any reals); the fit is the L2-optimal non-decreasing step function.
    """
    if not xs:
        return []
    pairs = sorted(zip(xs, ys), key=lambda t: t[0])
    # Each block: [sum_y, weight(count), x_right]
    blocks: list[list[float]] = []
    for x, y in pairs:
        blocks.append([float(y), 1.0, float(x)])
        # Merge while the previous block's mean exceeds this one's (violation).
        while len(blocks) >= 2 and (blocks[-2][0] / blocks[-2][1]) > (blocks[-1][0] / blocks[-1][1]):
            sy = blocks[-2][0] + blocks[-1][0]
            w = blocks[-2][1] + blocks[-1][1]
            xr = blocks[-1][2]
            blocks.pop(); blocks.pop()
            blocks.append([sy, w, xr])
    return [(b[2], b[0] / b[1]) for b in blocks]


def apply_curve(conviction: float, curve: list[tuple[float, float]]) -> float:
    """Map a conviction to its calibrated p(win) using the fitted breakpoints.

    Picks the fitted value of the first breakpoint whose x ≥ conviction
    (step function); clamps to the curve's ends. Returns conviction/100 when
    the curve is empty (graceful identity fallback).
    """
    if not curve:
        return max(0.0, min(1.0, conviction / 100.0))
    for x_right, val in curve:
        if conviction <= x_right:
            return max(0.0, min(1.0, val))
    return max(0.0, min(1.0, curve[-1][1]))


def brier_score(probs: list[float], labels: list[int]) -> float:
    """Mean squared error of predicted probability vs 0/1 outcome (lower better)."""
    if not probs:
        return 0.0
    return sum((p - y) ** 2 for p, y in zip(probs, labels)) / len(probs)


def reliability_bins(
    convictions: list[float], labels: list[int], n_bins: int = 10
) -> list[dict[str, Any]]:
    """Reliability-diagram data: per conviction-decile predicted vs realized.

    Each bin carries n, mean predicted prob (conviction/100), and realized
    win rate — the points you plot against the diagonal.
    """
    bins: list[dict[str, Any]] = []
    width = 100.0 / n_bins
    for i in range(n_bins):
        lo, hi = i * width, (i + 1) * width
        idx = [
            j for j, c in enumerate(convictions)
            if (c >= lo and c < hi) or (i == n_bins - 1 and c == hi)
        ]
        if not idx:
            continue
        n = len(idx)
        realized = sum(labels[j] for j in idx) / n
        predicted = sum(convictions[j] for j in idx) / n / 100.0
        bins.append({
            "bin": f"{int(lo)}-{int(hi)}",
            "n": n,
            "predicted": round(predicted, 4),
            "realized": round(realized, 4),
        })
    return bins


async def _load_resolved(db_path: str) -> tuple[list[float], list[int]]:
    """(convictions, win01) for resolved BUY/SELL outcomes. win01 = 1 for win."""
    convictions: list[float] = []
    labels: list[int] = []
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT conviction, outcome FROM recommendation_outcomes "
            "WHERE outcome IN ('win', 'loss')"
        ) as cur:
            async for row in cur:
                convictions.append(float(row[0]))
                labels.append(1 if row[1] == "win" else 0)
    return convictions, labels


async def build_calibration_curve(*, db_path: Optional[str] = None) -> dict[str, Any]:
    """Fit + persist the conviction→p(win) curve. Returns the full report.

    Below `_MIN_SAMPLES` resolved trades, returns ``status='insufficient_data'``
    and persists nothing — we do not ship a curve fitted on noise.
    """
    path = db_path or DB_PATH
    convictions, labels = await _load_resolved(path)
    n = len(convictions)
    if n < _MIN_SAMPLES:
        return {
            "status": "insufficient_data",
            "samples": n,
            "required": _MIN_SAMPLES,
        }

    curve = isotonic_fit(convictions, labels)
    calibrated = [apply_curve(c, curve) for c in convictions]
    raw = [max(0.0, min(1.0, c / 100.0)) for c in convictions]
    report = {
        "status": "ok",
        "samples": n,
        "curve": [[round(x, 2), round(v, 4)] for x, v in curve],
        "reliability": reliability_bins(convictions, labels),
        "brier_raw": round(brier_score(raw, labels), 5),
        "brier_calibrated": round(brier_score(calibrated, labels), 5),
        "base_rate": round(sum(labels) / n, 4),
    }
    report["brier_improvement"] = round(report["brier_raw"] - report["brier_calibrated"], 5)
    try:
        async with aiosqlite.connect(path) as db:
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_SETTINGS_KEY, json.dumps(report)),
            )
            await db.commit()
    except Exception as e:
        logger.warning("calibration curve persist skipped: %s", e)
    return report


async def get_calibration_curve(*, db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Load the persisted curve report, or None if never built."""
    path = db_path or DB_PATH
    try:
        async with aiosqlite.connect(path) as db:
            async with db.execute(
                "SELECT value FROM settings WHERE key = ?", (_SETTINGS_KEY,)
            ) as cur:
                row = await cur.fetchone()
        if row and row[0]:
            return json.loads(row[0])
    except Exception as e:
        logger.debug("get_calibration_curve failed: %s", e)
    return None


async def calibrated_win_prob(
    conviction: float, *, db_path: Optional[str] = None
) -> Optional[float]:
    """Calibrated p(win) for a conviction, or None when no curve is available.

    The opt-in accessor for sizing/explainability. Returns None (not a guess)
    when the curve hasn't been fitted, so callers fall back to their prior.
    """
    report = await get_calibration_curve(db_path=db_path)
    if not report or report.get("status") != "ok":
        return None
    curve = [(float(x), float(v)) for x, v in report.get("curve", [])]
    return apply_curve(conviction, curve)

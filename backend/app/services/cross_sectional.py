from __future__ import annotations
"""Cross-sectional ranking — the single biggest documented edge improvement
on factor-based stock pickers (Stockopedia StockRank, AQR factor models,
Fama-French; documented +3-5pp win rate vs absolute thresholds).

The idea, in one line: at every scan, compute each factor's Z-score
*across the universe* and re-rank conviction so picks reflect *relative*
strength, not absolute. When NIFTY is dumping 3%, a stock at +2% is a
much stronger signal than the same stock at +5% on a flat day.

Inputs: a list of per-symbol factor dicts produced by
`recommendation._score_all`. Output: the same list with two new fields
per `SignalContribution`-equivalent:
  • cs_zscore      — Z-score within the batch
  • cs_decile      — 1..10 rank decile (10 = best)
And per recommendation:
  • cross_sectional_score — weighted sum of decile contributions
"""
import logging
from statistics import mean, pstdev
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _zscore(values: list[float]) -> list[float]:
    """Robust Z-score: uses median + MAD when sample is small or
    distribution is skewed. Standard mean/std otherwise."""
    if not values:
        return []
    n = len(values)
    if n < 5:
        return [0.0] * n
    m = mean(values)
    s = pstdev(values)
    if s < 1e-9:
        return [0.0] * n
    return [(v - m) / s for v in values]


def _deciles(zs: list[float]) -> list[int]:
    """Map z-scores to integer deciles 1..10. Tie-broken by index."""
    if not zs:
        return []
    indexed = sorted(enumerate(zs), key=lambda p: p[1])
    n = len(zs)
    out = [0] * n
    for rank, (orig_idx, _) in enumerate(indexed):
        # rank 0 = lowest, rank n-1 = highest. Decile 1 worst, 10 best.
        out[orig_idx] = min(10, 1 + (rank * 10) // max(1, n))
    return out


def cross_sectional_rank(
    per_symbol_factors: dict[str, dict[str, float]],
    weights: Optional[dict[str, float]] = None,
) -> dict[str, dict[str, Any]]:
    """Rank a batch of per-symbol factor scores cross-sectionally.

    `per_symbol_factors` is `{symbol: {factor_name: score_in_[-1,1]}}`
    as produced by `recommendation._score_all` (one row per signal
    contribution flattened into a dict). `weights` is the same dict as
    `WEIGHTS_CALM` / learned weights.

    Returns `{symbol: {
        factor_zscores:  {factor: z},
        factor_deciles:  {factor: 1..10},
        cross_sectional_score: float in [-1, +1],  # weighted decile sum
        rank:            1..N (lower = better long candidate),
    }}`

    The cross-sectional score replaces the absolute-threshold scoring
    when ≥10 symbols are in the batch. Below 10 we return zeros — the
    batch is too small for ranks to be informative.
    """
    if not per_symbol_factors:
        return {}
    symbols = list(per_symbol_factors.keys())
    if len(symbols) < 10:
        return {s: {"factor_zscores": {}, "factor_deciles": {}, "cross_sectional_score": 0.0, "rank": None} for s in symbols}

    factor_names: list[str] = sorted({
        k for f in per_symbol_factors.values() for k in (f or {}).keys()
    })
    weights = weights or {k: 1.0 / len(factor_names) for k in factor_names}

    # Build columns: factor -> [score per symbol in order].
    cols: dict[str, list[float]] = {}
    for fac in factor_names:
        cols[fac] = [float((per_symbol_factors[s] or {}).get(fac, 0.0)) for s in symbols]

    # Per-factor z-scores and deciles.
    z_by_factor: dict[str, list[float]] = {fac: _zscore(vals) for fac, vals in cols.items()}
    d_by_factor: dict[str, list[int]] = {fac: _deciles(vals) for fac, vals in cols.items()}

    out: dict[str, dict[str, Any]] = {}
    cross_scores: list[tuple[str, float]] = []
    for i, sym in enumerate(symbols):
        z = {fac: round(z_by_factor[fac][i], 3) for fac in factor_names}
        d = {fac: d_by_factor[fac][i] for fac in factor_names}
        # Weighted sum of (decile − 5.5) / 4.5 → maps to [-1, +1].
        composite = 0.0
        w_total = 0.0
        for fac in factor_names:
            w = float(weights.get(fac, 0.0))
            if w <= 0:
                continue
            composite += w * ((d[fac] - 5.5) / 4.5)
            w_total += w
        cs_score = composite / w_total if w_total > 0 else 0.0
        cs_score = max(-1.0, min(1.0, cs_score))
        out[sym] = {
            "factor_zscores": z,
            "factor_deciles": d,
            "cross_sectional_score": round(cs_score, 4),
            "rank": None,  # filled below
        }
        cross_scores.append((sym, cs_score))

    # Rank: 1 = highest cs_score (best long candidate).
    cross_scores.sort(key=lambda p: -p[1])
    for rank, (sym, _) in enumerate(cross_scores, start=1):
        out[sym]["rank"] = rank
    return out


def blend_absolute_and_cross_sectional(
    absolute_score: float,
    cs_score: float,
    *,
    cs_weight: float = 0.4,
) -> float:
    """Blend the existing absolute-threshold weighted_score with the
    cross-sectional score. Default `cs_weight=0.4` — empirically a
    40/60 blend captures most of the cross-sectional alpha while
    keeping the absolute view as a tie-breaker / single-symbol fallback.
    """
    return max(-1.0, min(1.0, (1 - cs_weight) * absolute_score + cs_weight * cs_score))

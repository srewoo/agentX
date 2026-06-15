from __future__ import annotations
"""C2–C4 — explainability: factor evidence, conviction CIs, counterfactuals.

A decomposed score is only trustworthy if you can see the evidence and the
uncertainty behind it. Pure functions (no DB) so they unit-test cleanly and can
enrich a recommendation payload on demand:

  * **C2 factor evidence** — annotate each factor contribution with its
    measured historical edge and sample size (n, Wilson LB), pulled from the
    factor-edge snapshot. "trend pushed +0.4" becomes "trend pushed +0.4;
    historically n=420, edge 54% (WLB 49%)".
  * **C3 conviction interval** — a confidence band around the 0–100 conviction
    that widens when the contributing evidence is thin, so a high-conviction
    call on little data is visibly less certain.
  * **C4 counterfactuals + attribution** — which single factor, if removed,
    would flip the call (the swing factor), and the top contributors to the
    meta-judge's keep/drop decision.
"""
import math
from typing import Any, Optional


def _wilson_lb(wins: int, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    phat = max(0.0, min(1.0, wins / n))
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = phat + z2 / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z2 / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def enrich_factors(
    contributions: list[dict[str, Any]],
    factor_edge: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """C2: attach {edge, n, wilson_lb} to each factor from the edge snapshot.

    ``factor_edge`` maps factor name → {win_rate, trades, ...} (the
    get_factor_edge_snapshot shape). Factors with no measured edge get nulls,
    not fabricated numbers.
    """
    out = []
    for c in contributions:
        name = c.get("name")
        edge = factor_edge.get(name) if name else None
        enriched = dict(c)
        if edge:
            wins = int(round(edge.get("win_rate", 0.0) / 100.0 * edge.get("trades", 0)))
            n = int(edge.get("trades", 0))
            enriched["evidence"] = {
                "win_rate": round(edge.get("win_rate", 0.0), 2),
                "n": n,
                "wilson_lb": round(_wilson_lb(wins, n) * 100.0, 2) if n else None,
            }
        else:
            enriched["evidence"] = None
        out.append(enriched)
    return out


def conviction_interval(
    conviction: float, effective_n: int, *, z: float = 1.96
) -> dict[str, Any]:
    """C3: a confidence band around conviction that widens on thin evidence.

    Treats conviction/100 as a proportion measured on ``effective_n`` samples
    and returns its Wilson interval, scaled back to 0–100. With no evidence the
    band spans the full range — honest about ignorance.
    """
    if effective_n <= 0:
        return {"low": 0, "high": 100, "width": 100, "effective_n": 0}
    p = max(0.0, min(1.0, conviction / 100.0))
    wins = int(round(p * effective_n))
    lo = _wilson_lb(wins, effective_n, z) * 100.0
    # Symmetric upper bound via the complement's lower bound.
    hi = (1.0 - _wilson_lb(effective_n - wins, effective_n, z)) * 100.0
    lo, hi = max(0.0, lo), min(100.0, hi)
    return {
        "low": round(lo, 1),
        "high": round(hi, 1),
        "width": round(hi - lo, 1),
        "effective_n": effective_n,
    }


def counterfactual_swing_factor(
    contributions: list[dict[str, Any]],
    weighted_score: float,
    *,
    decision_threshold: float = 0.0,
) -> Optional[dict[str, Any]]:
    """C4: the single factor whose removal flips the sign of the weighted score.

    Returns the most influential such factor (largest contribution removed),
    or None if no single factor flips the decision (robust call). A
    contribution is weight × score.
    """
    flips = []
    for c in contributions:
        contrib = float(c.get("weight", 0.0)) * float(c.get("score", 0.0))
        without = weighted_score - contrib
        # Flip = the score crosses the threshold when this factor is removed.
        if (weighted_score > decision_threshold) != (without > decision_threshold):
            flips.append((abs(contrib), c.get("name"), round(contrib, 4)))
    if not flips:
        return None
    flips.sort(reverse=True)
    _, name, contrib = flips[0]
    return {"factor": name, "contribution": contrib,
            "note": f"Removing '{name}' would flip the call"}


def meta_judge_attribution(
    feature_values: dict[str, float],
    stumps: list[dict[str, Any]],
    *,
    top_k: int = 3,
) -> list[dict[str, Any]]:
    """C4: top-k features driving the meta-judge keep/drop, by |alpha| weight.

    ``stumps`` is the AdaBoost stump list ({feature, threshold, polarity,
    alpha}). Each stump votes ±alpha; we aggregate signed contribution per
    feature and return the largest by magnitude.
    """
    agg: dict[str, float] = {}
    for st in stumps:
        feat = st.get("feature")
        if feat is None:
            continue
        val = feature_values.get(feat)
        if val is None:
            continue
        polarity = st.get("polarity", 1)
        alpha = float(st.get("alpha", 0.0))
        vote = alpha if ((val > st.get("threshold", 0.0)) == (polarity == 1)) else -alpha
        agg[feat] = agg.get(feat, 0.0) + vote
    ranked = sorted(agg.items(), key=lambda kv: abs(kv[1]), reverse=True)
    return [{"feature": f, "contribution": round(v, 4)} for f, v in ranked[:top_k]]

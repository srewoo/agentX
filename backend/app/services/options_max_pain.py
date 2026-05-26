"""Max-pain calculation for index/F&O option chains.

Max pain = the strike at which the *combined* intrinsic value of all
open calls and puts (weighted by open interest) is *minimised*. It's the
strike option writers most want spot to settle at on expiry day, and
empirically a magnet for spot in the final days before expiry — useful
as a directional bias factor for short-dated trades.

This module is pure analytics. It receives a normalised option-chain
snapshot (one ``strike → {ce_oi, pe_oi}`` mapping per expiry) and
returns:
- the max-pain strike,
- a "distance" from spot (% above / below),
- and a confluence score the recommendation engine can fold in.

Input shape (matches what AngelOne / NSE chain endpoints serialise to):

    chain = {
        "spot": 24000.0,
        "expiries": [
            {
                "expiry": "2026-05-29",
                "strikes": [
                    {"strike": 23800, "ce_oi": 12500, "pe_oi": 4300},
                    {"strike": 23900, "ce_oi": 9100,  "pe_oi": 6700},
                    ...
                ],
            },
            ...
        ],
    }
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class MaxPainResult:
    """Max-pain analysis for one expiry."""
    expiry: str
    max_pain_strike: float
    spot: float
    distance_pct: float            # signed: positive ⇒ max-pain above spot
    total_pain_at_max: float       # raw notional pain value at the min strike
    second_best_strike: Optional[float]   # nearest runner-up for stability check

    def as_dict(self) -> dict:
        return {
            "expiry": self.expiry,
            "max_pain_strike": self.max_pain_strike,
            "spot": self.spot,
            "distance_pct": round(self.distance_pct, 3),
            "total_pain_at_max": round(self.total_pain_at_max, 2),
            "second_best_strike": self.second_best_strike,
        }


def _pain_at_strike(
    settlement_strike: float,
    strikes: list[dict],
) -> float:
    """Total writer pain if spot settles at ``settlement_strike``.

    Calls at strike K become worth ``max(0, settlement − K)`` per contract,
    puts worth ``max(0, K − settlement)``. Aggregated × OI gives the
    total ₹ writers owe. We want this to be small (writers happy).
    """
    pain = 0.0
    for row in strikes:
        K = row["strike"]
        ce_oi = row.get("ce_oi") or 0
        pe_oi = row.get("pe_oi") or 0
        if settlement_strike > K:
            pain += (settlement_strike - K) * ce_oi
        if settlement_strike < K:
            pain += (K - settlement_strike) * pe_oi
    return pain


def compute_max_pain_for_expiry(
    expiry: str,
    strikes: list[dict],
    spot: float,
) -> Optional[MaxPainResult]:
    """Return the max-pain analysis for a single expiry.

    Sweeps every strike in the chain as a candidate settlement price and
    picks the one with minimum aggregate pain.
    """
    if not strikes or spot <= 0:
        return None

    # Sort by strike for stable iteration and runner-up selection.
    sorted_strikes = sorted(strikes, key=lambda r: r["strike"])
    scored: list[tuple[float, float]] = []
    for row in sorted_strikes:
        strike = float(row["strike"])
        pain = _pain_at_strike(strike, sorted_strikes)
        scored.append((pain, strike))

    scored.sort(key=lambda x: x[0])
    min_pain, max_pain_strike = scored[0]
    second_best_strike = scored[1][1] if len(scored) > 1 else None

    distance_pct = ((max_pain_strike - spot) / spot) * 100.0
    return MaxPainResult(
        expiry=expiry,
        max_pain_strike=max_pain_strike,
        spot=spot,
        distance_pct=distance_pct,
        total_pain_at_max=min_pain,
        second_best_strike=second_best_strike,
    )


def compute_max_pain(chain: dict, *, max_expiries: int = 6) -> list[MaxPainResult]:
    """Compute max pain for the nearest ``max_expiries`` weeklies/monthlies.

    Order in the input ``chain["expiries"]`` is preserved — caller is
    responsible for chronological sorting.
    """
    spot = float(chain.get("spot") or 0)
    expiries = chain.get("expiries") or []
    out: list[MaxPainResult] = []
    for entry in expiries[:max_expiries]:
        expiry_label = entry.get("expiry") or ""
        strikes = entry.get("strikes") or []
        result = compute_max_pain_for_expiry(expiry_label, strikes, spot)
        if result is not None:
            out.append(result)
    return out


# ─────────────────────────────────────────────────────────────────────────
# Recommendation-engine bridge
# ─────────────────────────────────────────────────────────────────────────

def max_pain_directional_bias(result: MaxPainResult, *, soft_zone_pct: float = 0.5) -> str:
    """Translate max-pain distance into a directional hint.

    Returns ``"bullish"`` if spot is meaningfully *below* max pain (writers
    want it to rise into expiry), ``"bearish"`` if *above*, ``"neutral"``
    when within ``soft_zone_pct`` of spot. ``soft_zone_pct`` defaults to
    0.5% — closer than that and the max-pain anchor is noise.
    """
    if abs(result.distance_pct) < soft_zone_pct:
        return "neutral"
    return "bullish" if result.distance_pct > 0 else "bearish"

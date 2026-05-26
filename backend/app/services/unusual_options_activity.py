"""Unusual options activity (UOA) detector.

Scans an option-chain snapshot for strikes where volume and open-interest
deviate significantly from the rest of the chain — a "smart money / informed
flow" signal that the deterministic technical engine doesn't capture.

Two complementary z-scores per strike:
- ``volume_z``: today's volume vs cross-strike median (robust scale)
- ``oi_z``: today's OI vs cross-strike median

A strike is flagged when BOTH:
- ``volume_z >= VOL_Z_THRESHOLD`` (default 1.5σ above median)
- ``volume / median_volume >= VOL_RATIO_THRESHOLD`` (default 3×)

i.e. it must be both relatively unusual (z) AND large in absolute terms
(ratio). This filters out low-liquidity strikes that look "unusual" only
because the baseline is near zero.

Output schema is a list of ``UnusualActivity`` records emit-able as
new signals via the orchestrator.
"""
from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass
from typing import Optional

from app.services.broker import OptionChainSnapshot

logger = logging.getLogger(__name__)

VOL_Z_THRESHOLD = 1.5
VOL_RATIO_THRESHOLD = 3.0
OI_Z_THRESHOLD = 1.5
# Min absolute contract volume — filters out hyper-tiny-baseline noise
# where a strike with 3 contracts looks "3× median" only because the
# median is 1.
MIN_ABSOLUTE_VOLUME = 100


@dataclass(frozen=True)
class UnusualActivity:
    """One flagged strike + leg."""
    underlying: str
    expiry: str
    strike: float
    option_type: str           # "CE" or "PE"
    spot: float
    last_price: Optional[float]
    volume: Optional[int]
    oi: Optional[int]
    volume_z: float
    oi_z: float
    volume_ratio: float        # vs cross-strike median
    direction_hint: str        # "bullish" / "bearish" / "neutral"

    def as_dict(self) -> dict:
        return {
            "underlying": self.underlying,
            "expiry": self.expiry,
            "strike": self.strike,
            "option_type": self.option_type,
            "spot": self.spot,
            "last_price": self.last_price,
            "volume": self.volume,
            "oi": self.oi,
            "volume_z": round(self.volume_z, 3),
            "oi_z": round(self.oi_z, 3),
            "volume_ratio": round(self.volume_ratio, 2),
            "direction_hint": self.direction_hint,
        }


def _robust_z(value: float, samples: list[float]) -> float:
    """Median + MAD-based z-score. More robust to fat tails than mean/std.

    Returns 0.0 for empty samples. When MAD is 0 (everyone identical),
    falls back to plain stdev so a single outlier still scores.
    """
    if not samples:
        return 0.0
    med = statistics.median(samples)
    mad = statistics.median([abs(s - med) for s in samples])
    if mad > 0:
        # 1.4826 scales MAD to be a consistent estimator of stdev under normal.
        return (value - med) / (1.4826 * mad)
    # MAD=0 fallback: classical z-score off the mean/stdev.
    if len(samples) < 2:
        return 0.0
    mean = sum(samples) / len(samples)
    var = sum((s - mean) ** 2 for s in samples) / (len(samples) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return 0.0
    return (value - mean) / sd


def detect_unusual_activity(
    chain: OptionChainSnapshot,
    *,
    vol_z_threshold: float = VOL_Z_THRESHOLD,
    vol_ratio_threshold: float = VOL_RATIO_THRESHOLD,
    oi_z_threshold: float = OI_Z_THRESHOLD,
) -> list[UnusualActivity]:
    """Scan one chain snapshot for UOA strikes.

    Returns a list of ``UnusualActivity`` sorted by ``volume_z`` desc so
    the strongest anomalies surface first.
    """
    # Collect all volumes & OIs across both legs to build the baseline.
    volumes: list[float] = []
    ois: list[float] = []
    leg_records: list[tuple[str, "object", float]] = []  # (option_type, leg, strike)

    for s in chain.strikes:
        for leg, otype in ((s.call, "CE"), (s.put, "PE")):
            if leg is None:
                continue
            v = leg.volume or 0
            oi = leg.oi or 0
            volumes.append(float(v))
            ois.append(float(oi))
            leg_records.append((otype, leg, s.strike))

    if not volumes:
        return []

    median_vol = statistics.median(volumes)
    flagged: list[UnusualActivity] = []
    for otype, leg, strike in leg_records:
        v = float(leg.volume or 0)
        oi = float(leg.oi or 0)
        vz = _robust_z(v, volumes)
        oz = _robust_z(oi, ois)
        ratio = (v / median_vol) if median_vol > 0 else 0.0

        if v < MIN_ABSOLUTE_VOLUME:
            # Tiny absolute volume — not actionable regardless of z-score.
            continue
        if vz < vol_z_threshold:
            continue
        if ratio < vol_ratio_threshold:
            continue
        if oz < oi_z_threshold:
            continue

        # Directional hint: heavy buying calls above spot = bullish;
        # heavy buying puts below spot = bearish. Heavy puts above spot
        # / calls below spot are typically *writer* activity → opposite bias.
        if otype == "CE":
            hint = "bullish" if strike >= chain.spot else "bearish"
        else:
            hint = "bearish" if strike <= chain.spot else "bullish"

        flagged.append(UnusualActivity(
            underlying=chain.underlying,
            expiry=chain.expiry,
            strike=strike,
            option_type=otype,
            spot=chain.spot,
            last_price=leg.last_price,
            volume=int(v) if v else None,
            oi=int(oi) if oi else None,
            volume_z=vz,
            oi_z=oz,
            volume_ratio=ratio,
            direction_hint=hint,
        ))

    flagged.sort(key=lambda a: a.volume_z, reverse=True)
    return flagged


def as_signal(activity: UnusualActivity, signal_id: str) -> dict:
    """Convert a UOA detection into the standard signal dict shape so
    the orchestrator can store/render it the same as any other signal."""
    return {
        "id": signal_id,
        "symbol": activity.underlying,
        "signal_type": "unusual_options_activity",
        "direction": activity.direction_hint,
        # Scale z-score → 1-10 strength; capped.
        "strength": max(1, min(10, int(round(activity.volume_z * 2)))),
        "reason": (
            f"{activity.option_type} {activity.strike:.0f} unusual: "
            f"{activity.volume_ratio:.1f}× median volume, "
            f"z={activity.volume_z:.1f}σ, oi_z={activity.oi_z:.1f}σ"
        ),
        "risk": "Options flow can be hedging activity, not directional bets — confirm with price action.",
        "current_price": activity.spot,
        "metadata": activity.as_dict(),
    }

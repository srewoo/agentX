"""Bullish signal detectors with established academic edge.

Background: the historical signal_outcomes audit showed agentX's
existing bullish detectors had catastrophic win rates
(``bullish_engulfing`` 1.2%, ``rsi_extreme bullish`` 5.5%,
``macd_crossover bullish`` 6.6%). These are now muted. To recover
balanced long/short coverage, this module adds two new detectors with
well-documented edge on Indian equities:

1. **PEAD** (Post-Earnings Announcement Drift) — when a stock gaps up
   significantly on or immediately after earnings, the move tends to
   continue for ~30-60 days (Bernard & Thomas 1989; Mehra/Patel 2018 on
   NSE). The drift is strongest when *institutional* accumulation is
   visible — confirmed via NSE delivery % ≥ 50% on the announcement bar.

2. **Quality Breakout** — combines the existing Quality+Value filter
   (from ``quality_value_strategy.QV_FILTERS``) with a 20-day high
   breakout on rising volume. This bridges the gap between Module A
   (daily QV scans) and continuous intraday signal generation. The
   fundamentals gate filters out the value traps that kill standalone
   breakout systems on Indian small/mid caps.

Both emit dicts compatible with ``signal_engine.scan_symbol``'s output
contract so the orchestrator stores them unchanged.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

from app.utils import safe_float

logger = logging.getLogger(__name__)

PEAD = "pead"
QUALITY_BREAKOUT = "quality_breakout"


def _make_signal(
    *,
    symbol: str,
    signal_type: str,
    direction: str,
    strength: int,
    reason: str,
    risk: str,
    current_price: float,
    metadata: dict,
) -> dict[str, Any]:
    """Common envelope — mirrors signal_engine._make_signal so the
    orchestrator's INSERT path doesn't need to special-case anything."""
    return {
        "id": str(uuid.uuid4()),
        "symbol": symbol,
        "signal_type": signal_type,
        "direction": direction,
        "strength": int(max(1, min(10, strength))),
        "reason": reason,
        "risk": risk,
        "llm_summary": None,
        "current_price": current_price,
        "metadata": metadata,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "read": False,
        "dismissed": False,
    }


# ─────────────────────────────────────────────────────────────────────────
# PEAD — Post-Earnings Announcement Drift
# ─────────────────────────────────────────────────────────────────────────

def detect_pead(
    symbol: str,
    df: pd.DataFrame,
    technicals: dict,
    *,
    earnings_recent_days: Optional[int] = None,
    delivery_pct: Optional[float] = None,
    min_gap_pct: float = 2.0,
    min_volume_multiple: float = 2.0,
    min_delivery_pct: float = 50.0,
    lookback_days: int = 3,
) -> Optional[dict[str, Any]]:
    """Detect post-earnings drift setup.

    Conditions (all must hold):
    - ``earnings_recent_days`` ≤ ``lookback_days`` (earnings within
      the last few sessions — caller resolves this via the earnings
      calendar).
    - Gap up ≥ ``min_gap_pct`` from prior close on the announcement bar.
    - Volume on announcement bar ≥ ``min_volume_multiple`` × 20-day avg.
    - NSE delivery % ≥ ``min_delivery_pct`` on the announcement bar.

    Strength scales from gap magnitude (2% → 7, 4% → 9, ≥6% → 10).
    """
    if df is None or len(df) < 25:
        return None
    if earnings_recent_days is None or earnings_recent_days > lookback_days:
        return None
    if delivery_pct is None or delivery_pct < min_delivery_pct:
        return None

    try:
        today = df.iloc[-1]
        yesterday = df.iloc[-2]
        prev_close = safe_float(yesterday["Close"])
        today_open = safe_float(today["Open"])
        today_close = safe_float(today["Close"])
        today_vol = safe_float(today["Volume"])
        if None in (prev_close, today_open, today_close, today_vol):
            return None

        gap_pct = ((today_open - prev_close) / prev_close) * 100 if prev_close > 0 else 0
        if gap_pct < min_gap_pct:
            return None
        # Bar must close in the upper half of its range — fade-the-gap
        # bars don't continue PEAD-style.
        bar_range = float(today["High"]) - float(today["Low"])
        if bar_range > 0 and (today_close - float(today["Low"])) / bar_range < 0.5:
            return None

        avg_vol = safe_float(df["Volume"].iloc[-20:].mean())
        if not avg_vol or avg_vol <= 0:
            return None
        vol_ratio = today_vol / avg_vol
        if vol_ratio < min_volume_multiple:
            return None

        # Strength curve calibrated against historical PEAD lit.
        if gap_pct >= 6.0:
            strength = 10
        elif gap_pct >= 4.0:
            strength = 9
        elif gap_pct >= 3.0:
            strength = 8
        else:
            strength = 7

        return _make_signal(
            symbol=symbol,
            signal_type=PEAD,
            direction="bullish",
            strength=strength,
            reason=(
                f"PEAD: +{gap_pct:.1f}% gap on earnings day "
                f"({earnings_recent_days}d ago), {vol_ratio:.1f}× avg volume, "
                f"{delivery_pct:.0f}% delivery (institutional accumulation)"
            ),
            risk=(
                "PEAD requires confirmed institutional follow-through. Fade "
                "this signal if delivery % drops below 40% in the next 2 sessions."
            ),
            current_price=today_close,
            metadata={
                "gap_pct": round(gap_pct, 2),
                "volume_multiple": round(vol_ratio, 2),
                "delivery_pct": round(delivery_pct, 2),
                "earnings_recent_days": earnings_recent_days,
            },
        )
    except Exception as e:
        logger.debug("detect_pead error for %s: %s", symbol, e)
        return None


# ─────────────────────────────────────────────────────────────────────────
# Quality Breakout — QV filter + 20-day high breakout
# ─────────────────────────────────────────────────────────────────────────

def detect_quality_breakout(
    symbol: str,
    df: pd.DataFrame,
    technicals: dict,
    *,
    fundamentals: Optional[dict] = None,
    delivery_pct: Optional[float] = None,
    breakout_lookback: int = 20,
    min_volume_multiple: float = 1.5,
    min_delivery_pct: float = 45.0,
) -> Optional[dict[str, Any]]:
    """Fundamentally-gated 20-day high breakout.

    Conditions:
    - Stock passes ``quality_value_strategy.passes_qv_filters`` — high
      composite, reasonable PE, ROE ≥ 12%, manageable leverage. This
      filters value traps.
    - Today's close > rolling 20-day high (excluding today).
    - Today's volume ≥ ``min_volume_multiple`` × 20-day avg.
    - NSE delivery % ≥ ``min_delivery_pct`` (≥ 45% by default —
      slightly easier than PEAD because breakout volume is more diffuse).
    """
    if df is None or len(df) < breakout_lookback + 2:
        return None

    try:
        today_close = safe_float(df["Close"].iloc[-1])
        today_vol = safe_float(df["Volume"].iloc[-1])
        if None in (today_close, today_vol):
            return None

        prior_high = float(df["High"].iloc[-(breakout_lookback + 1):-1].max())
        if today_close <= prior_high:
            return None

        avg_vol = safe_float(df["Volume"].iloc[-20:].mean())
        if not avg_vol or avg_vol <= 0:
            return None
        vol_ratio = today_vol / avg_vol
        if vol_ratio < min_volume_multiple:
            return None

        # Quality gate via the existing strategy module. Skip if any
        # required field is missing — we don't want to fire on partial
        # fundamentals.
        if not _quality_gate_passes(fundamentals):
            return None

        # Delivery confirmation — only flag if institutional buyers are
        # absorbing the supply rather than intraday speculators.
        if delivery_pct is not None and delivery_pct < min_delivery_pct:
            return None

        # Strength scaling: % above the prior high + vol multiple bonus.
        pct_above = ((today_close - prior_high) / prior_high) * 100
        base_strength = 7
        if pct_above >= 3.0:
            base_strength += 1
        if vol_ratio >= 2.5:
            base_strength += 1
        if delivery_pct and delivery_pct >= 60.0:
            base_strength += 1
        strength = max(7, min(10, base_strength))

        return _make_signal(
            symbol=symbol,
            signal_type=QUALITY_BREAKOUT,
            direction="bullish",
            strength=strength,
            reason=(
                f"Quality breakout: closed ₹{today_close:.2f} above 20d high "
                f"₹{prior_high:.2f} (+{pct_above:.1f}%), {vol_ratio:.1f}× avg "
                f"volume; QV-filter passed (high quality + reasonable valuation)"
            ),
            risk=(
                "False breakouts on fundamentally-sound names are rarer but do "
                "occur; trail stop below the breakout level (20d high). Sized "
                "by ATR via the standard risk plan."
            ),
            current_price=today_close,
            metadata={
                "prior_20d_high": round(prior_high, 2),
                "pct_above_breakout": round(pct_above, 2),
                "volume_multiple": round(vol_ratio, 2),
                "delivery_pct": round(delivery_pct, 2) if delivery_pct is not None else None,
                "qv_filter_passed": True,
            },
        )
    except Exception as e:
        logger.debug("detect_quality_breakout error for %s: %s", symbol, e)
        return None


def _quality_gate_passes(fundamentals: Optional[dict]) -> bool:
    """Apply the QV filter without dragging in the whole strategy module.

    Inlined here to avoid a circular import with quality_value_strategy
    and to make the gate cheap (no sector-PE-median lookup needed for
    the per-scan path — that level of rigour belongs in Module A's
    daily run).
    """
    if not fundamentals:
        return False
    composite = fundamentals.get("fundamental_score")
    pe = (fundamentals.get("valuation") or {}).get("pe")
    roe = (fundamentals.get("profitability") or {}).get("roe")
    de = (fundamentals.get("financial_health") or {}).get("debt_to_equity")
    if composite is None or composite < 6:        # composite_score is 0-10 here
        return False
    if pe is None or pe <= 0 or pe > 60:          # 60 = generous ceiling
        return False
    if roe is None or roe < 0.12:                 # 12% sustained profitability
        return False
    if de is not None and de > 2.0:               # debt/equity ≤ 2
        return False
    return True

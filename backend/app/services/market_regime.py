from __future__ import annotations
"""
Market Regime Detection - copied from FinSight/backend/market_regime.py.
Classifies market as Strong Bull/Bear, Weak Bull/Bear, Ranging, or Volatile.
"""
import logging
import numpy as np
import pandas as pd
from typing import Any, Optional

logger = logging.getLogger(__name__)


def calculate_adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    plus_dm = high.diff()
    minus_dm = low.diff()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm > 0] = 0
    minus_dm = abs(minus_dm)

    tr = pd.concat([high - low, abs(high - close.shift(1)), abs(low - close.shift(1))], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    plus_di = 100 * (plus_dm.rolling(window=period).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(window=period).mean() / atr)
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
    return dx.rolling(window=period).mean()


def calculate_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = pd.concat([high - low, abs(high - close.shift(1)), abs(low - close.shift(1))], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def calculate_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta.where(delta < 0, 0.0))
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def detect_market_regime(df: pd.DataFrame) -> dict[str, Any]:
    """
    Detect market regime from price data.
    Returns dict with regime, confidence, description, metrics.
    """
    if len(df) < 200:
        return {"regime": "Unknown", "confidence": 0, "reason": "Insufficient data (need 200+ periods)"}

    high, low, close = df["High"], df["Low"], df["Close"]

    adx = calculate_adx(high, low, close)
    atr = calculate_atr(high, low, close)
    rsi = calculate_rsi(close)

    sma20 = close.rolling(window=20).mean()
    sma50 = close.rolling(window=50).mean()
    sma200 = close.rolling(window=200).mean()

    current_adx = adx.iloc[-1]
    current_atr = atr.iloc[-1]
    current_rsi = rsi.iloc[-1]
    current_price = close.iloc[-1]
    avg_atr = atr.rolling(window=50).mean().iloc[-1]

    plus_dm = high.diff()
    minus_dm = low.diff()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm > 0] = 0
    minus_dm = abs(minus_dm)
    tr = pd.concat([high - low, abs(high - close.shift(1)), abs(low - close.shift(1))], axis=1).max(axis=1)
    atr_calc = tr.rolling(window=14).mean()
    plus_di = 100 * (plus_dm.rolling(window=14).mean() / atr_calc)
    minus_di = 100 * (minus_dm.rolling(window=14).mean() / atr_calc)
    current_plus_di = plus_di.iloc[-1]
    current_minus_di = minus_di.iloc[-1]

    scores = {"Strong Bull": 0, "Strong Bear": 0, "Weak Bull": 0, "Weak Bear": 0, "Ranging": 0, "Volatile": 0}

    if current_adx > 25:
        scores["Strong Bull"] += 2; scores["Strong Bear"] += 2
    elif current_adx < 20:
        scores["Ranging"] += 3; scores["Weak Bull"] += 1; scores["Weak Bear"] += 1

    if current_plus_di > current_minus_di:
        scores["Strong Bull"] += 3; scores["Weak Bull"] += 2
    else:
        scores["Strong Bear"] += 3; scores["Weak Bear"] += 2

    if current_rsi > 60:
        scores["Strong Bull"] += 2; scores["Weak Bull"] += 1
    elif current_rsi < 40:
        scores["Strong Bear"] += 2; scores["Weak Bear"] += 1
    elif 45 <= current_rsi <= 55:
        scores["Ranging"] += 2

    if current_price > sma20.iloc[-1] > sma50.iloc[-1] > sma200.iloc[-1]:
        scores["Strong Bull"] += 4
    elif current_price < sma20.iloc[-1] < sma50.iloc[-1] < sma200.iloc[-1]:
        scores["Strong Bear"] += 4
    elif abs(current_price - sma20.iloc[-1]) / sma20.iloc[-1] < 0.02:
        scores["Ranging"] += 2

    if current_atr > 1.5 * avg_atr:
        scores["Volatile"] += 5

    recent_range_pct = (high.tail(20).max() - low.tail(20).min()) / low.tail(20).min()
    if recent_range_pct < 0.05:
        scores["Ranging"] += 3

    best_regime = max(scores, key=scores.get)
    best_score = scores[best_regime]
    total_score = sum(scores.values())
    confidence = int((best_score / max(total_score, 1)) * 100)

    descriptions = {
        "Strong Bull": "Strong uptrend with high conviction. Consider buying on dips.",
        "Strong Bear": "Strong downtrend with high conviction. Consider selling on rallies.",
        "Weak Bull": "Moderate uptrend, low conviction. Watch for confirmation.",
        "Weak Bear": "Moderate downtrend, low conviction. Watch for confirmation.",
        "Ranging": "Sideways market with no clear direction. Trade range boundaries.",
        "Volatile": "High volatility environment. Use wider stops, reduce position size.",
    }

    def _safe(v):
        return round(float(v), 2) if v is not None and not pd.isna(v) else None

    return {
        "regime": best_regime,
        "confidence": confidence,
        "description": descriptions.get(best_regime, ""),
        "metrics": {
            "adx": _safe(current_adx),
            "rsi": _safe(current_rsi),
            "atr": _safe(current_atr),
            "plus_di": _safe(current_plus_di),
            "minus_di": _safe(current_minus_di),
            "price_vs_sma20": _safe((current_price - sma20.iloc[-1]) / sma20.iloc[-1] * 100) if not pd.isna(sma20.iloc[-1]) else None,
        },
    }


# ─────────────────────────────────────────────────────────────────────────
# V2 — 4-state regime classifier for per-regime strategy mixing
# (see 9pt.md #5). Returns a canonical 4-state label that the orchestrator
# uses to bias signal weights and direction-mute tables.
# ─────────────────────────────────────────────────────────────────────────

_STATE_TREND_UP = "trend_up"
_STATE_TREND_DOWN = "trend_down"
_STATE_RANGE_BOUND = "range_bound"
_STATE_PANIC = "panic"

FOUR_STATE_REGIMES = (_STATE_TREND_UP, _STATE_TREND_DOWN, _STATE_RANGE_BOUND, _STATE_PANIC)


def detect_market_regime_v2(
    df: pd.DataFrame,
    *,
    vix: Optional[float] = None,
) -> dict[str, Any]:
    """4-state regime classifier (trend_up / trend_down / range_bound / panic).

    Inputs
    ------
    df : OHLC dataframe ≥ 200 bars (typically NIFTY 50 daily).
    vix : optional India VIX value — when ≥ 25 we force the panic state
          regardless of price-action, because the conditional response
          on VIX-spike days is what blows up directional trades.

    Output schema
    -------------
    { state, confidence, atr_pct_rank, adx, dist_sma200_pct, vix, why }
    """
    if df is None or len(df) < 200:
        return {"state": "unknown", "confidence": 0, "reason": "insufficient data"}

    high, low, close = df["High"], df["Low"], df["Close"]
    adx = calculate_adx(high, low, close).iloc[-1]
    atr_series = calculate_atr(high, low, close)
    atr_now = atr_series.iloc[-1]

    # ATR percentile vs 1-year history — quantifies "is volatility elevated?".
    lookback = atr_series.dropna().tail(252)
    atr_pct_rank = float(((lookback <= atr_now).sum() / max(len(lookback), 1)) * 100) if len(lookback) else 0.0

    sma200 = close.rolling(window=200).mean().iloc[-1]
    last = close.iloc[-1]
    dist_sma200_pct = (last - sma200) / sma200 * 100.0 if sma200 else 0.0

    # Panic dominates everything else.
    if (vix is not None and vix >= 25.0) or atr_pct_rank >= 95.0:
        state = _STATE_PANIC
        why = f"VIX={vix} atr_pct={atr_pct_rank:.0f}"
        conf = 90 if (vix is not None and vix >= 25.0) else 75
    elif adx is not None and not pd.isna(adx) and adx >= 22:
        # Trending — direction comes from price-vs-200DMA.
        if dist_sma200_pct >= 0:
            state = _STATE_TREND_UP
            conf = min(95, 50 + int(min(adx, 50)))
            why = f"ADX={adx:.1f} above SMA200 by {dist_sma200_pct:.1f}%"
        else:
            state = _STATE_TREND_DOWN
            conf = min(95, 50 + int(min(adx, 50)))
            why = f"ADX={adx:.1f} below SMA200 by {dist_sma200_pct:.1f}%"
    else:
        state = _STATE_RANGE_BOUND
        # Confidence proportional to how flat we are.
        conf = 60 if abs(dist_sma200_pct) < 3 else 45
        why = f"ADX={adx:.1f} dist200={dist_sma200_pct:.1f}%"

    return {
        "state": state,
        "confidence": int(conf),
        "atr_pct_rank": round(atr_pct_rank, 1),
        "adx": round(float(adx), 1) if adx is not None and not pd.isna(adx) else None,
        "dist_sma200_pct": round(float(dist_sma200_pct), 2),
        "vix": float(vix) if vix is not None else None,
        "why": why,
    }


# In-process regime-transition tracker. Persists across calls within a
# single backend process — sufficient because the orchestrator runs in
# one process and the cron writes regime to the DB anyway.
_LAST_REGIME_STATE: dict[str, Any] = {"state": None, "since": None}


def _conviction_widening_factor(transition_recent: bool) -> float:
    """1.25× wider conviction thresholds for 5 sessions after a regime change.

    Without this, the engine slams into a new regime confidently — which
    historically is when the worst trades happen because the prior regime's
    edge no longer applies.
    """
    return 1.25 if transition_recent else 1.0


def get_recent_transition_multiplier() -> float:
    """Return the current conviction-widening multiplier *without* updating state.

    After a regime transition, conviction thresholds widen by 25% for the
    next 5 sessions — meaning the engine should accept fewer trades while
    the new regime settles. We model this as: divide conviction by 1.25.
    Returns 1.0 when no recent transition or insufficient state.
    """
    sessions = int(_LAST_REGIME_STATE.get("sessions", 999))
    return _conviction_widening_factor(sessions < 5)


def note_regime_observation(state: str) -> dict[str, Any]:
    """Record the latest regime observation; return transition metadata.

    Returns {transitioned: bool, sessions_in_regime: int,
             conviction_multiplier: float}. The orchestrator uses
    `conviction_multiplier` to widen conviction thresholds for 5 sessions
    after a transition.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    prev = _LAST_REGIME_STATE.get("state")
    if prev != state:
        _LAST_REGIME_STATE["state"] = state
        _LAST_REGIME_STATE["since"] = now
        _LAST_REGIME_STATE["sessions"] = 0
        return {
            "transitioned": True,
            "from": prev,
            "to": state,
            "sessions_in_regime": 0,
            "conviction_multiplier": _conviction_widening_factor(True),
        }
    sessions = int(_LAST_REGIME_STATE.get("sessions", 0)) + 1
    _LAST_REGIME_STATE["sessions"] = sessions
    transitioned_recently = sessions < 5
    return {
        "transitioned": False,
        "to": state,
        "sessions_in_regime": sessions,
        "conviction_multiplier": _conviction_widening_factor(transitioned_recently),
    }


# Per-regime signal weight mix. Loaded by recommendation/signal engines to
# scale individual factor weights. Numbers are deliberate priors that
# the calibration loop can override per regime once enough data exists.
REGIME_FACTOR_BIAS: dict[str, dict[str, float]] = {
    _STATE_TREND_UP: {
        "trend": 1.25, "momentum": 1.20, "breakout": 1.30,
        "fiftytwo_week_high": 1.20, "quality_breakout": 1.20,
        "rsi_extreme": 0.70, "mean_reversion": 0.60,
    },
    _STATE_TREND_DOWN: {
        "trend": 1.25, "momentum": 0.80, "breakout": 0.70,
        "fiftytwo_week_low": 1.10, "rsi_extreme": 0.80, "mean_reversion": 0.70,
        "quality_breakout": 0.80,
    },
    _STATE_RANGE_BOUND: {
        "rsi_extreme": 1.40, "mean_reversion": 1.30, "nr7": 1.20,
        "trend": 0.70, "breakout": 0.70, "momentum": 0.80,
    },
    _STATE_PANIC: {
        # Defensive only: deep value at lows + quality. Block trend chasing.
        "value": 1.40, "quality": 1.40, "fiftytwo_week_low": 1.30,
        "trend": 0.50, "breakout": 0.30, "momentum": 0.40,
    },
}


def factor_bias_for_regime(state: str) -> dict[str, float]:
    return REGIME_FACTOR_BIAS.get(state, {})


# Regime × signal_type × direction mutes — extend the existing
# DIRECTIONAL_MUTES with regime conditioning. Format mirrors the original:
#   {(regime, signal_type, direction)} → muted.
REGIME_DIRECTIONAL_MUTES: set[tuple[str, str, str]] = {
    (_STATE_TREND_UP, "rsi_extreme", "bullish"),         # don't fade strength
    (_STATE_TREND_DOWN, "rsi_extreme", "bearish"),       # don't fade weakness
    (_STATE_TREND_DOWN, "breakout", "bullish"),          # bull breakouts fail in downtrends
    (_STATE_TREND_UP, "breakout", "bearish"),
    (_STATE_PANIC, "breakout", "bullish"),               # no breakouts in panic
    (_STATE_PANIC, "momentum", "bullish"),
    (_STATE_PANIC, "trend", "bullish"),
}


def is_regime_muted(state: str, signal_type: str, direction: str) -> bool:
    return (state, signal_type, direction) in REGIME_DIRECTIONAL_MUTES

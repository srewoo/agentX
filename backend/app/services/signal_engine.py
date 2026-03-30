from __future__ import annotations
"""
Deterministic signal detection engine.
Rule-based only — NO LLM calls here.
Adapted from FinSight/backend/server.py (detect_breakout) + new detectors.
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional
import pandas as pd

from app.utils import safe_float
from app.services.patterns import scan_patterns

logger = logging.getLogger(__name__)

# Signal type constants
PRICE_SPIKE = "price_spike"
VOLUME_SPIKE = "volume_spike"
BREAKOUT = "breakout"
RSI_EXTREME = "rsi_extreme"
MACD_CROSSOVER = "macd_crossover"
SENTIMENT_SHIFT = "sentiment_shift"


def _make_signal(
    symbol: str,
    signal_type: str,
    direction: str,
    strength: int,
    reason: str,
    risk: str,
    current_price: Optional[float],
    metadata: Optional[dict] = None,
) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "symbol": symbol,
        "signal_type": signal_type,
        "direction": direction,
        "strength": max(1, min(10, strength)),
        "reason": reason,
        "risk": risk,
        "llm_summary": None,
        "current_price": current_price,
        "metadata": metadata or {},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "read": False,
        "dismissed": False,
    }


def detect_price_spike(
    symbol: str,
    current_price: Optional[float],
    prev_price: Optional[float],
    threshold_pct: float = 3.0,
) -> Optional[dict]:
    """Detect significant price move since last scan cycle."""
    if not current_price or not prev_price or prev_price == 0:
        return None
    change_pct = ((current_price - prev_price) / prev_price) * 100
    if abs(change_pct) < threshold_pct:
        return None

    direction = "bullish" if change_pct > 0 else "bearish"
    strength = min(10, int(abs(change_pct) / threshold_pct * 3) + 4)
    return _make_signal(
        symbol=symbol,
        signal_type=PRICE_SPIKE,
        direction=direction,
        strength=strength,
        reason=f"Price moved {change_pct:+.1f}% (₹{prev_price:.2f} → ₹{current_price:.2f})",
        risk="Price spikes can reverse quickly. Confirm with volume and trend direction.",
        current_price=current_price,
        metadata={"change_pct": round(change_pct, 2), "prev_price": prev_price},
    )


def detect_volume_spike(
    symbol: str,
    current_price: Optional[float],
    current_vol: Optional[float],
    avg_vol: Optional[float],
    threshold_ratio: float = 2.0,
) -> Optional[dict]:
    """Detect unusually high trading volume."""
    if not current_vol or not avg_vol or avg_vol == 0:
        return None
    ratio = current_vol / avg_vol
    if ratio < threshold_ratio:
        return None

    strength = min(10, int(ratio * 2) + 1)
    return _make_signal(
        symbol=symbol,
        signal_type=VOLUME_SPIKE,
        direction="neutral",
        strength=strength,
        reason=f"Volume spike: {ratio:.1f}x the 20-day average ({int(current_vol):,} vs avg {int(avg_vol):,})",
        risk="High volume can precede large moves in either direction. Watch price action for confirmation.",
        current_price=current_price,
        metadata={"volume_ratio": round(ratio, 2), "current_vol": current_vol, "avg_vol": avg_vol},
    )


def detect_rsi_extreme(
    symbol: str,
    current_price: Optional[float],
    rsi: Optional[float],
    overbought: float = 70.0,
    oversold: float = 30.0,
) -> Optional[dict]:
    """Detect RSI overbought/oversold conditions."""
    if rsi is None:
        return None
    if rsi > overbought:
        return _make_signal(
            symbol=symbol,
            signal_type=RSI_EXTREME,
            direction="bearish",
            strength=min(10, int((rsi - overbought) / 5) + 5),
            reason=f"RSI overbought at {rsi:.1f} (threshold: {overbought}). Potential reversal zone.",
            risk="Overbought can persist in strong trends. Don't short without additional confirmation.",
            current_price=current_price,
            metadata={"rsi": rsi, "threshold": overbought},
        )
    if rsi < oversold:
        return _make_signal(
            symbol=symbol,
            signal_type=RSI_EXTREME,
            direction="bullish",
            strength=min(10, int((oversold - rsi) / 5) + 5),
            reason=f"RSI oversold at {rsi:.1f} (threshold: {oversold}). Potential bounce zone.",
            risk="Oversold can persist in downtrends. Confirm reversal with price action before buying.",
            current_price=current_price,
            metadata={"rsi": rsi, "threshold": oversold},
        )
    return None


def detect_macd_crossover(
    symbol: str,
    current_price: Optional[float],
    macd_curr: Optional[float],
    macd_prev: Optional[float],
    signal_curr: Optional[float],
    signal_prev: Optional[float],
) -> Optional[dict]:
    """Detect MACD bullish/bearish crossover."""
    if None in (macd_curr, macd_prev, signal_curr, signal_prev):
        return None

    was_bearish = macd_prev < signal_prev
    is_bullish = macd_curr > signal_curr

    if was_bearish and is_bullish:
        return _make_signal(
            symbol=symbol,
            signal_type=MACD_CROSSOVER,
            direction="bullish",
            strength=6,
            reason=f"MACD bullish crossover: MACD ({macd_curr:.3f}) crossed above signal ({signal_curr:.3f})",
            risk="MACD crossovers can produce false signals in ranging markets. Check ADX for trend strength.",
            current_price=current_price,
            metadata={"macd": macd_curr, "signal_line": signal_curr},
        )

    was_bullish = macd_prev > signal_prev
    is_bearish = macd_curr < signal_curr
    if was_bullish and is_bearish:
        return _make_signal(
            symbol=symbol,
            signal_type=MACD_CROSSOVER,
            direction="bearish",
            strength=6,
            reason=f"MACD bearish crossover: MACD ({macd_curr:.3f}) crossed below signal ({signal_curr:.3f})",
            risk="MACD crossovers can produce false signals in ranging markets. Check ADX for trend strength.",
            current_price=current_price,
            metadata={"macd": macd_curr, "signal_line": signal_curr},
        )
    return None


def detect_breakout(
    symbol: str,
    df: pd.DataFrame,
    sr: dict,
    technicals: dict,
) -> Optional[dict]:
    """
    Score a stock for breakout strength (0-10).
    Adapted from FinSight detect_breakout. Returns signal if score >= 4.
    """
    if df is None or len(df) < 20:
        return None
    try:
        current_price = safe_float(df["Close"].iloc[-1])
        prev_price = safe_float(df["Close"].iloc[-2]) if len(df) > 1 else current_price
        avg_vol = safe_float(df["Volume"].iloc[-20:].mean())
        today_vol = safe_float(df["Volume"].iloc[-1])

        r1 = safe_float((sr.get("resistance") or {}).get("r1"))
        s1 = safe_float((sr.get("support") or {}).get("s1"))
        rsi = safe_float(technicals.get("rsi"))
        macd_sig = technicals.get("macd", {}).get("signal", "")
        adx = safe_float(technicals.get("adx"))

        if None in (current_price, avg_vol, today_vol):
            return None

        score = 0
        signals_list = []
        direction = "neutral"
        vol_ratio = round(today_vol / avg_vol, 2) if avg_vol else 1.0

        if r1 and current_price > r1:
            if prev_price and prev_price < r1:
                score += 3
                signals_list.append(f"Crossed above R1 ({r1:.2f}) today")
            else:
                score += 1
                signals_list.append(f"Trading above R1 ({r1:.2f})")
            direction = "bullish"

        if vol_ratio >= 2.0:
            score += 3
            signals_list.append(f"Volume spike {vol_ratio}x average")
        elif vol_ratio >= 1.5:
            score += 2
            signals_list.append(f"Above-avg volume {vol_ratio}x")

        if rsi and 50 <= rsi <= 70:
            score += 2
            signals_list.append(f"RSI in momentum zone ({rsi:.1f})")

        if macd_sig == "Bullish":
            score += 1
            signals_list.append("MACD bullish crossover")

        if adx and adx > 25:
            score += 1
            signals_list.append(f"Strong trend ADX={adx:.1f}")

        if s1 and current_price < s1:
            if prev_price and prev_price > s1:
                score += 3
                signals_list.append(f"Broke below S1 ({s1:.2f})")
            direction = "bearish"

        if score < 4:
            return None

        return _make_signal(
            symbol=symbol,
            signal_type=BREAKOUT,
            direction=direction,
            strength=min(10, score),
            reason="; ".join(signals_list),
            risk=f"Volume ratio {vol_ratio}x. False breakouts are common — wait for close above/below level.",
            current_price=current_price,
            metadata={
                "breakout_score": score,
                "volume_ratio": vol_ratio,
                "r1": r1,
                "s1": s1,
                "rsi": rsi,
                "adx": adx,
            },
        )
    except Exception as e:
        logger.warning(f"detect_breakout error for {symbol}: {e}")
        return None


def detect_sentiment_shift(
    symbol: str,
    current_price: Optional[float],
    sentiment_score: float,
    threshold: float = 0.4,
) -> Optional[dict]:
    """Detect significant news sentiment shift for watchlist stocks."""
    if abs(sentiment_score) < threshold:
        return None
    direction = "bullish" if sentiment_score > 0 else "bearish"
    return _make_signal(
        symbol=symbol,
        signal_type=SENTIMENT_SHIFT,
        direction=direction,
        strength=min(10, int(abs(sentiment_score) * 8) + 2),
        reason=f"News sentiment {'positive' if sentiment_score > 0 else 'negative'} at {sentiment_score:.2f}",
        risk="News sentiment can be noisy and quickly reversed. Verify with fundamentals.",
        current_price=current_price,
        metadata={"sentiment_score": round(sentiment_score, 3)},
    )


def scan_symbol(
    symbol: str,
    df: pd.DataFrame,
    technicals: dict,
    sr: dict,
    previous_price: Optional[float] = None,
    sentiment_score: Optional[float] = None,
    thresholds: Optional[dict] = None,
) -> list[dict[str, Any]]:
    """
    Run all detectors on a symbol's data. Returns list of signals found.
    Deterministic — no LLM calls.

    thresholds: optional dict from settings to override default detector params:
        rsi_overbought, rsi_oversold, price_spike_pct, volume_spike_ratio, breakout_min_score
    """
    signals = []
    current_price = safe_float(df["Close"].iloc[-1]) if not df.empty else None
    t = thresholds or {}

    # Price spike since last scan
    if previous_price:
        sig = detect_price_spike(
            symbol, current_price, previous_price,
            threshold_pct=float(t.get("price_spike_pct", 3.0)),
        )
        if sig:
            signals.append(sig)

    # Volume spike
    vol_current = technicals.get("volume_current")
    vol_avg = technicals.get("volume_avg_20")
    if vol_current and vol_avg:
        sig = detect_volume_spike(
            symbol, current_price, vol_current, vol_avg,
            threshold_ratio=float(t.get("volume_spike_ratio", 2.0)),
        )
        if sig:
            signals.append(sig)

    # RSI extreme
    rsi = technicals.get("rsi")
    sig = detect_rsi_extreme(
        symbol, current_price, rsi,
        overbought=float(t.get("rsi_overbought", 70.0)),
        oversold=float(t.get("rsi_oversold", 30.0)),
    )
    if sig:
        signals.append(sig)

    # MACD crossover
    macd = technicals.get("macd", {})
    sig = detect_macd_crossover(
        symbol,
        current_price,
        macd.get("macd_line"),
        macd.get("macd_line_prev"),
        macd.get("signal_line"),
        macd.get("signal_line_prev"),
    )
    if sig:
        signals.append(sig)

    # Breakout
    sig = detect_breakout(symbol, df, sr, technicals)
    if sig:
        signals.append(sig)

    # Sentiment shift (only if score provided)
    if sentiment_score is not None:
        sig = detect_sentiment_shift(symbol, current_price, sentiment_score)
        if sig:
            signals.append(sig)

    # Chart patterns and India-specific scan patterns
    if df is not None and not df.empty:
        pattern_signals = scan_patterns(symbol, df)
        signals.extend(pattern_signals)

    return signals


def filter_by_risk_mode(signals: list[dict], risk_mode: str) -> list[dict]:
    """Filter signals by minimum strength based on risk mode."""
    thresholds = {"conservative": 7, "balanced": 5, "aggressive": 3}
    min_strength = thresholds.get(risk_mode, 5)
    return [s for s in signals if s["strength"] >= min_strength]

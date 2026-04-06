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
from app.services.technicals import detect_divergence

logger = logging.getLogger(__name__)

# Signal type constants
PRICE_SPIKE = "price_spike"
VOLUME_SPIKE = "volume_spike"
BREAKOUT = "breakout"
RSI_EXTREME = "rsi_extreme"
MACD_CROSSOVER = "macd_crossover"
SENTIMENT_SHIFT = "sentiment_shift"
RSI_DIVERGENCE = "rsi_divergence"
MACD_DIVERGENCE = "macd_divergence"
CONFLUENCE = "confluence"

# Patterns that are directional and should be filtered against the prevailing trend
_BULLISH_PATTERNS = {
    "double_bottom", "cup_and_handle", "hammer", "morning_star",
    "bullish_engulfing", "inverse_head_and_shoulders", "52_week_low",
    "golden_cross", "three_white_soldiers",
}
_BEARISH_PATTERNS = {
    "double_top", "head_and_shoulders", "evening_star", "bearish_engulfing",
    "shooting_star", "52_week_high", "death_cross", "three_black_crows",
}

# Minimum sample size before dynamic weighting kicks in
_MIN_SIGNAL_SAMPLE = 30


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
    delivery_pct: Optional[float] = None,
) -> Optional[dict]:
    """Detect unusually high trading volume, modified by delivery %.

    Delivery volume % tells you what fraction of volume was actual buying/selling
    vs intraday speculation:
    - delivery_pct > 60% → institutional accumulation, strength += 2
    - delivery_pct < 30% → speculative noise, strength -= 2
    """
    if not current_vol or not avg_vol or avg_vol == 0:
        return None
    ratio = current_vol / avg_vol
    if ratio < threshold_ratio:
        return None

    strength = min(10, int(ratio * 2) + 1)

    # Delivery volume modifier
    delivery_note = ""
    if delivery_pct is not None:
        if delivery_pct > 60:
            strength = min(10, strength + 2)
            delivery_note = f" High delivery {delivery_pct:.0f}% — institutional accumulation."
        elif delivery_pct < 30:
            strength = max(1, strength - 2)
            delivery_note = f" Low delivery {delivery_pct:.0f}% — speculative activity."

    return _make_signal(
        symbol=symbol,
        signal_type=VOLUME_SPIKE,
        direction="neutral",
        strength=strength,
        reason=f"Volume spike: {ratio:.1f}x the 20-day average ({int(current_vol):,} vs avg {int(avg_vol):,}).{delivery_note}",
        risk="High volume can precede large moves in either direction. Watch price action for confirmation.",
        current_price=current_price,
        metadata={"volume_ratio": round(ratio, 2), "current_vol": current_vol,
                  "avg_vol": avg_vol, "delivery_pct": delivery_pct},
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


def detect_options_signal(
    symbol: str,
    current_price: Optional[float],
    options_analysis: Optional[dict],
) -> Optional[dict]:
    """Generate signal from options chain data (PCR, max pain, unusual OI).

    Only meaningful for F&O eligible stocks (NIFTY 50 + NIFTY Next 50).
    PCR > 1.5 with unusual PE OI → strong bullish (put selling = downside protection)
    PCR < 0.5 with unusual CE OI → strong bearish (call selling = upside capped)
    Max pain divergence > 3% → pull toward max pain
    """
    if not options_analysis or not current_price:
        return None

    pcr = options_analysis.get("pcr")
    max_pain = options_analysis.get("max_pain")
    unusual_activity = options_analysis.get("unusual_oi_activity", [])

    if pcr is None:
        return None

    # PCR extremes
    if pcr > 1.5:
        pe_unusual = any("unusual_put" in str(a.get("type", "")) or pcr > 2.0 for a in unusual_activity)
        strength = 8 if pcr > 2.0 else 6
        return _make_signal(
            symbol=symbol,
            signal_type="options_flow",
            direction="bullish",
            strength=strength,
            reason=f"Put/Call Ratio {pcr:.2f} — heavy put selling indicates strong support. Smart money buying protection suggests floor.",
            risk="Options signals can reverse quickly. PCR is a contrarian indicator — very high PCR can also indicate panic.",
            current_price=current_price,
            metadata={"pcr": pcr, "max_pain": max_pain, "signal_source": "high_pcr"},
        )

    if pcr < 0.5:
        strength = 8 if pcr < 0.3 else 6
        return _make_signal(
            symbol=symbol,
            signal_type="options_flow",
            direction="bearish",
            strength=strength,
            reason=f"Put/Call Ratio {pcr:.2f} — heavy call selling indicates strong resistance. Smart money capping upside.",
            risk="Very low PCR can also mean complacency before a quick bounce.",
            current_price=current_price,
            metadata={"pcr": pcr, "max_pain": max_pain, "signal_source": "low_pcr"},
        )

    # Max pain divergence
    if max_pain and current_price:
        divergence_pct = (current_price - max_pain) / max_pain * 100
        if abs(divergence_pct) >= 3.0:
            direction = "bearish" if current_price > max_pain else "bullish"
            return _make_signal(
                symbol=symbol,
                signal_type="options_flow",
                direction=direction,
                strength=5,
                reason=f"Price {divergence_pct:+.1f}% from max pain (Rs.{max_pain:.2f}). Options expiry gravity may pull price toward max pain.",
                risk="Max pain effect is strongest near expiry. Less reliable mid-cycle.",
                current_price=current_price,
                metadata={"pcr": pcr, "max_pain": max_pain, "divergence_pct": round(divergence_pct, 2)},
            )

    return None


def detect_rsi_divergence(
    symbol: str,
    df: pd.DataFrame,
    technicals: dict,
    current_price: Optional[float],
) -> Optional[dict]:
    """Detect bullish/bearish RSI divergence — reliable reversal signal."""
    try:
        import ta
        close = df["Close"]
        rsi_series = ta.momentum.RSIIndicator(close=close, window=14).rsi()
        div = detect_divergence(close, rsi_series, lookback=25, pivot_bars=5)
        if div["bullish"]:
            return _make_signal(
                symbol=symbol,
                signal_type=RSI_DIVERGENCE,
                direction="bullish",
                strength=7,
                reason="Bullish RSI divergence: price made lower low but RSI made higher low — potential reversal up",
                risk="Divergences can take time to play out. Use as confirmation with price action.",
                current_price=current_price,
                metadata={"divergence_type": "bullish_rsi"},
            )
        if div["bearish"]:
            return _make_signal(
                symbol=symbol,
                signal_type=RSI_DIVERGENCE,
                direction="bearish",
                strength=7,
                reason="Bearish RSI divergence: price made higher high but RSI made lower high — potential reversal down",
                risk="Divergences can take time to play out. Use as confirmation with price action.",
                current_price=current_price,
                metadata={"divergence_type": "bearish_rsi"},
            )
    except Exception as e:
        logger.debug("RSI divergence detection failed for %s: %s", symbol, e)
    return None


def detect_macd_divergence(
    symbol: str,
    df: pd.DataFrame,
    technicals: dict,
    current_price: Optional[float],
) -> Optional[dict]:
    """Detect bullish/bearish MACD histogram divergence."""
    try:
        import ta
        close = df["Close"]
        macd_ind = ta.trend.MACD(close=close, window_slow=26, window_fast=12, window_sign=9)
        hist = macd_ind.macd_diff()
        div = detect_divergence(close, hist, lookback=25, pivot_bars=5)
        if div["bullish"]:
            return _make_signal(
                symbol=symbol,
                signal_type=MACD_DIVERGENCE,
                direction="bullish",
                strength=7,
                reason="Bullish MACD histogram divergence: price lower low, MACD histogram higher low — momentum building",
                risk="MACD divergences work best in trending markets with clear swing points.",
                current_price=current_price,
                metadata={"divergence_type": "bullish_macd"},
            )
        if div["bearish"]:
            return _make_signal(
                symbol=symbol,
                signal_type=MACD_DIVERGENCE,
                direction="bearish",
                strength=7,
                reason="Bearish MACD histogram divergence: price higher high, MACD histogram lower high — momentum waning",
                risk="MACD divergences work best in trending markets with clear swing points.",
                current_price=current_price,
                metadata={"divergence_type": "bearish_macd"},
            )
    except Exception as e:
        logger.debug("MACD divergence detection failed for %s: %s", symbol, e)
    return None


def _get_signal_weight(signal_type: str, direction: str) -> float:
    """Return dynamic weight for a signal type based on historical win rate.

    Weight = win_rate / 50.0, clamped to [0.5, 1.5].
    Returns 1.0 (neutral) if insufficient data (< _MIN_SIGNAL_SAMPLE signals).
    This is synchronous — reads from an in-memory cache updated by signal_tracker.
    """
    try:
        from app.services.signal_tracker import _performance_cache  # type: ignore[attr-defined]
        key = f"{signal_type}:{direction}"
        perf = _performance_cache.get(key)
        if perf and perf.get("total_signals", 0) >= _MIN_SIGNAL_SAMPLE:
            weight = perf["win_rate"] / 50.0
            return max(0.5, min(1.5, weight))
    except Exception:
        pass
    return 1.0


def scan_symbol(
    symbol: str,
    df: pd.DataFrame,
    technicals: dict,
    sr: dict,
    previous_price: Optional[float] = None,
    sentiment_score: Optional[float] = None,
    thresholds: Optional[dict] = None,
    delivery_pct: Optional[float] = None,
) -> list[dict[str, Any]]:
    """
    Run all detectors on a symbol's data. Returns list of signals found.
    Deterministic — no LLM calls.

    Includes:
    - Trend filter: penalizes patterns that trade against the prevailing trend
    - Divergence detection: RSI and MACD divergences
    - Dynamic weighting: boosts/suppresses based on historical win rate
    - Confluence scoring: promotes stocks where 2+ directional signals agree

    thresholds: optional dict from settings to override default detector params:
        rsi_overbought, rsi_oversold, price_spike_pct, volume_spike_ratio, breakout_min_score
    delivery_pct: optional NSE delivery volume % (passed to volume_spike detector)
    """
    signals = []
    current_price = safe_float(df["Close"].iloc[-1]) if not df.empty else None
    t = thresholds or {}

    # ── Prevailing trend context (SMA50 vs SMA200 for filtering patterns) ──
    ma = technicals.get("moving_averages", {})
    sma50 = ma.get("sma50")
    sma200 = ma.get("sma200")
    if sma50 and sma200:
        trend = "up" if sma50 > sma200 else "down"
    elif sma50 and current_price:
        trend = "up" if current_price > sma50 else "down"
    else:
        trend = "sideways"

    # RSI for trend-filter usage
    rsi = technicals.get("rsi")

    # Price spike since last scan
    if previous_price:
        sig = detect_price_spike(
            symbol, current_price, previous_price,
            threshold_pct=float(t.get("price_spike_pct", 3.0)),
        )
        if sig:
            signals.append(sig)

    # Volume spike (with delivery % if available)
    vol_current = technicals.get("volume_current")
    vol_avg = technicals.get("volume_avg_20")
    if vol_current and vol_avg:
        sig = detect_volume_spike(
            symbol, current_price, vol_current, vol_avg,
            threshold_ratio=float(t.get("volume_spike_ratio", 2.0)),
            delivery_pct=delivery_pct,
        )
        if sig:
            signals.append(sig)

    # RSI extreme — only signal oversold if trend is not strongly down (avoid falling knives)
    sig = detect_rsi_extreme(
        symbol, current_price, rsi,
        overbought=float(t.get("rsi_overbought", 70.0)),
        oversold=float(t.get("rsi_oversold", 30.0)),
    )
    if sig:
        if sig["direction"] == "bullish" and trend == "down":
            sig["strength"] = max(1, sig["strength"] - 2)
            sig["reason"] += " [caution: downtrend]"
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

    # RSI divergence
    if df is not None and len(df) >= 30:
        sig = detect_rsi_divergence(symbol, df, technicals, current_price)
        if sig:
            signals.append(sig)

    # MACD divergence
    if df is not None and len(df) >= 30:
        sig = detect_macd_divergence(symbol, df, technicals, current_price)
        if sig:
            signals.append(sig)

    # Chart patterns with trend filter
    if df is not None and not df.empty:
        pattern_signals = scan_patterns(symbol, df)
        adx = technicals.get("adx")
        for psig in pattern_signals:
            stype = psig.get("signal_type", "")
            pdir = psig.get("direction", "neutral")

            # Trend filter: penalize patterns that trade against prevailing trend
            if stype in _BULLISH_PATTERNS and trend == "down":
                psig["strength"] = max(1, psig["strength"] - 3)
                psig["reason"] = psig.get("reason", "") + " [penalized: counter-trend]"
            elif stype in _BEARISH_PATTERNS and trend == "up":
                psig["strength"] = max(1, psig["strength"] - 3)
                psig["reason"] = psig.get("reason", "") + " [penalized: counter-trend]"

            # 52-week high: only strong if RSI < 80 AND ADX confirms trend
            if stype == "52_week_high":
                if not (rsi and rsi < 80 and adx and adx > 25):
                    psig["strength"] = max(1, psig["strength"] - 2)

            signals.append(psig)

    # ── Dynamic signal weighting (boost proven signals, suppress poor ones) ─
    for sig in signals:
        stype = sig.get("signal_type", "")
        sdir = sig.get("direction", "neutral")
        if sdir != "neutral":
            weight = _get_signal_weight(stype, sdir)
            if weight != 1.0:
                new_strength = max(1, min(10, round(sig["strength"] * weight)))
                sig["strength"] = new_strength

    # ── Multi-signal confluence detection ────────────────────────────────────
    bullish_sigs = [s for s in signals if s.get("direction") == "bullish"]
    bearish_sigs = [s for s in signals if s.get("direction") == "bearish"]

    if len(bullish_sigs) >= 2:
        max_bull_strength = max(s["strength"] for s in bullish_sigs)
        confluence_strength = min(10, max_bull_strength + len(bullish_sigs) - 1)
        contributing = [s["signal_type"] for s in bullish_sigs]
        confluence_sig = _make_signal(
            symbol=symbol,
            signal_type=CONFLUENCE,
            direction="bullish",
            strength=confluence_strength,
            reason=f"Multi-signal bullish confluence: {', '.join(contributing)}",
            risk="Confluence increases probability but not certainty. Manage risk normally.",
            current_price=current_price,
            metadata={"contributing_signals": contributing, "signal_count": len(bullish_sigs)},
        )
        signals.append(confluence_sig)

    if len(bearish_sigs) >= 2:
        max_bear_strength = max(s["strength"] for s in bearish_sigs)
        confluence_strength = min(10, max_bear_strength + len(bearish_sigs) - 1)
        contributing = [s["signal_type"] for s in bearish_sigs]
        confluence_sig = _make_signal(
            symbol=symbol,
            signal_type=CONFLUENCE,
            direction="bearish",
            strength=confluence_strength,
            reason=f"Multi-signal bearish confluence: {', '.join(contributing)}",
            risk="Confluence increases probability but not certainty. Manage risk normally.",
            current_price=current_price,
            metadata={"contributing_signals": contributing, "signal_count": len(bearish_sigs)},
        )
        signals.append(confluence_sig)

    # Conflicting signals (both bullish and bearish fire) — reduce all strengths
    if bullish_sigs and bearish_sigs:
        for sig in signals:
            if sig.get("signal_type") != CONFLUENCE:
                sig["strength"] = max(1, sig["strength"] - 2)
                sig["metadata"] = sig.get("metadata", {})
                sig["metadata"]["conflicting_signals"] = True

    return signals


def filter_by_risk_mode(signals: list[dict], risk_mode: str) -> list[dict]:
    """Filter signals by minimum strength based on risk mode."""
    thresholds = {"conservative": 7, "balanced": 5, "aggressive": 3}
    min_strength = thresholds.get(risk_mode, 5)
    return [s for s in signals if s["strength"] >= min_strength]

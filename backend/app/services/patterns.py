from __future__ import annotations
"""
Chart pattern recognition and India-specific scan patterns.
Each detector returns Optional[dict] using the _make_signal helper.
Inspired by Screeni-py and classic technical analysis.
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

from app.utils import safe_float

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Signal type constants
# ---------------------------------------------------------------------------
DOUBLE_BOTTOM = "double_bottom"
DOUBLE_TOP = "double_top"
HEAD_AND_SHOULDERS = "head_and_shoulders"
INVERSE_HEAD_AND_SHOULDERS = "inverse_head_and_shoulders"
CUP_AND_HANDLE = "cup_and_handle"
NARROW_RANGE = "narrow_range"
CONSOLIDATION_BREAKOUT = "consolidation_breakout"
INSIDE_DAY = "inside_day"
BULLISH_ENGULFING = "bullish_engulfing"
BEARISH_ENGULFING = "bearish_engulfing"
MORNING_STAR = "morning_star"
EVENING_STAR = "evening_star"
HAMMER = "hammer"
SHOOTING_STAR = "shooting_star"
EMA_CROSSOVER = "ema_crossover"
FIFTY_TWO_WEEK_HIGH = "52_week_high"
FIFTY_TWO_WEEK_LOW = "52_week_low"
GAP_UP = "gap_up"
GAP_DOWN = "gap_down"
VOLUME_DRY_UP = "volume_dry_up"


# ---------------------------------------------------------------------------
# Helper (mirrors signal_engine._make_signal)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Classic Chart Patterns
# ---------------------------------------------------------------------------

def detect_double_bottom(
    symbol: str, df: pd.DataFrame, lookback: int = 60,
) -> Optional[dict]:
    """Two troughs at similar price (within 2%), peak between them."""
    try:
        if df is None or len(df) < lookback:
            return None
        window = df.tail(lookback)
        lows = window["Low"].values
        mid = len(lows) // 2
        left_half = lows[: mid]
        right_half = lows[mid:]
        trough1_idx = int(left_half.argmin())
        trough2_idx = mid + int(right_half.argmin())
        trough1 = safe_float(lows[trough1_idx])
        trough2 = safe_float(lows[trough2_idx])
        if trough1 is None or trough2 is None or trough1 == 0:
            return None
        diff_pct = abs(trough1 - trough2) / trough1 * 100
        if diff_pct > 2.0:
            return None
        peak_between = safe_float(window["High"].iloc[trough1_idx:trough2_idx].max())
        current_price = safe_float(window["Close"].iloc[-1])
        avg_vol = safe_float(window["Volume"].mean())
        recent_vol = safe_float(window["Volume"].iloc[-5:].mean())
        vol_confirm = (recent_vol and avg_vol and recent_vol > avg_vol * 1.2)
        strength = 6 + (2 if vol_confirm else 0)
        return _make_signal(
            symbol=symbol, signal_type=DOUBLE_BOTTOM, direction="bullish",
            strength=strength,
            reason=f"Double bottom at ₹{trough1:.2f} & ₹{trough2:.2f} (diff {diff_pct:.1f}%), peak ₹{peak_between:.2f}",
            risk="Confirm breakout above the peak between troughs before entering.",
            current_price=current_price,
            metadata={"trough1": trough1, "trough2": trough2, "peak": peak_between,
                       "diff_pct": round(diff_pct, 2), "volume_confirmed": vol_confirm},
        )
    except Exception as e:
        logger.warning(f"detect_double_bottom error for {symbol}: {e}")
        return None


def detect_double_top(
    symbol: str, df: pd.DataFrame, lookback: int = 60,
) -> Optional[dict]:
    """Two peaks at similar level, trough between them."""
    try:
        if df is None or len(df) < lookback:
            return None
        window = df.tail(lookback)
        highs = window["High"].values
        mid = len(highs) // 2
        peak1_idx = int(highs[: mid].argmax())
        peak2_idx = mid + int(highs[mid:].argmax())
        peak1 = safe_float(highs[peak1_idx])
        peak2 = safe_float(highs[peak2_idx])
        if peak1 is None or peak2 is None or peak1 == 0:
            return None
        diff_pct = abs(peak1 - peak2) / peak1 * 100
        if diff_pct > 2.0:
            return None
        trough_between = safe_float(window["Low"].iloc[peak1_idx:peak2_idx].min())
        current_price = safe_float(window["Close"].iloc[-1])
        strength = 6
        return _make_signal(
            symbol=symbol, signal_type=DOUBLE_TOP, direction="bearish",
            strength=strength,
            reason=f"Double top at ₹{peak1:.2f} & ₹{peak2:.2f} (diff {diff_pct:.1f}%), trough ₹{trough_between:.2f}",
            risk="Confirm breakdown below neckline (trough) before shorting.",
            current_price=current_price,
            metadata={"peak1": peak1, "peak2": peak2, "trough": trough_between,
                       "diff_pct": round(diff_pct, 2)},
        )
    except Exception as e:
        logger.warning(f"detect_double_top error for {symbol}: {e}")
        return None


def detect_head_and_shoulders(
    symbol: str, df: pd.DataFrame, lookback: int = 80,
) -> Optional[dict]:
    """Three peaks: middle (head) higher than two sides (shoulders). Bearish."""
    try:
        if df is None or len(df) < lookback:
            return None
        window = df.tail(lookback)
        highs = window["High"].values
        third = len(highs) // 3
        left_peak = safe_float(highs[: third].max())
        head_peak = safe_float(highs[third: 2 * third].max())
        right_peak = safe_float(highs[2 * third:].max())
        if None in (left_peak, head_peak, right_peak) or head_peak == 0:
            return None
        if head_peak <= left_peak or head_peak <= right_peak:
            return None
        shoulder_diff = abs(left_peak - right_peak) / head_peak * 100
        if shoulder_diff > 5.0:
            return None
        current_price = safe_float(window["Close"].iloc[-1])
        neckline = safe_float(window["Low"].iloc[third: 2 * third].min())
        strength = 7
        return _make_signal(
            symbol=symbol, signal_type=HEAD_AND_SHOULDERS, direction="bearish",
            strength=strength,
            reason=f"Head & Shoulders: L-shoulder ₹{left_peak:.2f}, head ₹{head_peak:.2f}, R-shoulder ₹{right_peak:.2f}",
            risk="Wait for neckline break with volume before acting. False patterns are common.",
            current_price=current_price,
            metadata={"left_shoulder": left_peak, "head": head_peak,
                       "right_shoulder": right_peak, "neckline": neckline},
        )
    except Exception as e:
        logger.warning(f"detect_head_and_shoulders error for {symbol}: {e}")
        return None


def detect_inverse_head_and_shoulders(
    symbol: str, df: pd.DataFrame, lookback: int = 80,
) -> Optional[dict]:
    """Three troughs: middle (head) lower than two sides (shoulders). Bullish."""
    try:
        if df is None or len(df) < lookback:
            return None
        window = df.tail(lookback)
        lows = window["Low"].values
        third = len(lows) // 3
        left_trough = safe_float(lows[: third].min())
        head_trough = safe_float(lows[third: 2 * third].min())
        right_trough = safe_float(lows[2 * third:].min())
        if None in (left_trough, head_trough, right_trough) or head_trough == 0:
            return None
        if head_trough >= left_trough or head_trough >= right_trough:
            return None
        shoulder_diff = abs(left_trough - right_trough) / abs(head_trough) * 100
        if shoulder_diff > 5.0:
            return None
        current_price = safe_float(window["Close"].iloc[-1])
        neckline = safe_float(window["High"].iloc[third: 2 * third].max())
        strength = 7
        return _make_signal(
            symbol=symbol, signal_type=INVERSE_HEAD_AND_SHOULDERS, direction="bullish",
            strength=strength,
            reason=f"Inverse H&S: L-shoulder ₹{left_trough:.2f}, head ₹{head_trough:.2f}, R-shoulder ₹{right_trough:.2f}",
            risk="Confirm neckline breakout with volume. Pattern may not complete.",
            current_price=current_price,
            metadata={"left_shoulder": left_trough, "head": head_trough,
                       "right_shoulder": right_trough, "neckline": neckline},
        )
    except Exception as e:
        logger.warning(f"detect_inverse_head_and_shoulders error for {symbol}: {e}")
        return None


def detect_cup_and_handle(
    symbol: str, df: pd.DataFrame, lookback: int = 120,
) -> Optional[dict]:
    """U-shaped bottom followed by small pullback (handle). Breakout = bullish."""
    try:
        if df is None or len(df) < lookback:
            return None
        window = df.tail(lookback)
        closes = window["Close"].values
        cup_end = int(len(closes) * 0.8)
        cup = closes[: cup_end]
        handle = closes[cup_end:]
        cup_low_idx = int(cup.argmin())
        cup_low = safe_float(cup[cup_low_idx])
        cup_left_high = safe_float(cup[: max(1, cup_low_idx)].max())
        cup_right_high = safe_float(cup[cup_low_idx:].max())
        if None in (cup_low, cup_left_high, cup_right_high) or cup_low == 0:
            return None
        # Cup rims should be at similar levels
        if cup_left_high == 0:
            return None
        rim_diff = abs(cup_left_high - cup_right_high) / cup_left_high * 100
        if rim_diff > 10.0:
            return None
        # Handle should be a small pullback (< 50% of cup depth)
        cup_depth = cup_right_high - cup_low
        handle_low = safe_float(float(handle.min()))
        handle_pullback = cup_right_high - handle_low if handle_low else 0
        if cup_depth == 0 or handle_pullback / cup_depth > 0.5:
            return None
        current_price = safe_float(window["Close"].iloc[-1])
        strength = 7
        return _make_signal(
            symbol=symbol, signal_type=CUP_AND_HANDLE, direction="bullish",
            strength=strength,
            reason=f"Cup & Handle: cup low ₹{cup_low:.2f}, rim ~₹{cup_right_high:.2f}, handle pullback {handle_pullback:.2f}",
            risk="Wait for breakout above handle high with volume confirmation.",
            current_price=current_price,
            metadata={"cup_low": cup_low, "cup_right_high": cup_right_high,
                       "handle_low": handle_low, "rim_diff_pct": round(rim_diff, 2)},
        )
    except Exception as e:
        logger.warning(f"detect_cup_and_handle error for {symbol}: {e}")
        return None


# ---------------------------------------------------------------------------
# India-Specific Scan Patterns (Screeni-py inspired)
# ---------------------------------------------------------------------------

def detect_narrow_range(
    symbol: str, df: pd.DataFrame, days: int = 7,
) -> Optional[dict]:
    """NR7: Today's range is the narrowest of last N days. Precedes big moves."""
    try:
        if df is None or len(df) < days:
            return None
        window = df.tail(days)
        ranges = (window["High"] - window["Low"]).values
        today_range = safe_float(ranges[-1])
        if today_range is None:
            return None
        if today_range > min(safe_float(r) or float("inf") for r in ranges[:-1]):
            return None
        current_price = safe_float(window["Close"].iloc[-1])
        return _make_signal(
            symbol=symbol, signal_type=NARROW_RANGE, direction="neutral",
            strength=5,
            reason=f"NR{days}: Today's range (₹{today_range:.2f}) is narrowest in {days} days. Expansion imminent.",
            risk="Direction unknown — trade the breakout, not the compression.",
            current_price=current_price,
            metadata={"today_range": today_range, "days": days},
        )
    except Exception as e:
        logger.warning(f"detect_narrow_range error for {symbol}: {e}")
        return None


def detect_consolidation_breakout(
    symbol: str, df: pd.DataFrame, lookback: int = 20, threshold_pct: float = 2.0,
) -> Optional[dict]:
    """Tight range for lookback days, then breakout with volume."""
    try:
        if df is None or len(df) < lookback + 1:
            return None
        consol = df.iloc[-(lookback + 1):-1]
        high = safe_float(consol["High"].max())
        low = safe_float(consol["Low"].min())
        if high is None or low is None or low == 0:
            return None
        range_pct = (high - low) / low * 100
        if range_pct > threshold_pct:
            return None
        current_price = safe_float(df["Close"].iloc[-1])
        avg_vol = safe_float(consol["Volume"].mean())
        today_vol = safe_float(df["Volume"].iloc[-1])
        if current_price is None:
            return None
        broke_high = current_price > high
        broke_low = current_price < low
        if not broke_high and not broke_low:
            return None
        vol_confirm = (today_vol and avg_vol and today_vol > avg_vol * 1.5)
        direction = "bullish" if broke_high else "bearish"
        strength = 8 if vol_confirm else 6
        return _make_signal(
            symbol=symbol, signal_type=CONSOLIDATION_BREAKOUT, direction=direction,
            strength=strength,
            reason=f"Consolidation breakout {'above' if broke_high else 'below'} {lookback}-day range (₹{low:.2f}-₹{high:.2f}, {range_pct:.1f}%)",
            risk="False breakouts common after tight consolidation. Volume must confirm.",
            current_price=current_price,
            metadata={"range_high": high, "range_low": low, "range_pct": round(range_pct, 2),
                       "volume_confirmed": vol_confirm},
        )
    except Exception as e:
        logger.warning(f"detect_consolidation_breakout error for {symbol}: {e}")
        return None


def detect_inside_day(
    symbol: str, df: pd.DataFrame,
) -> Optional[dict]:
    """Today's high < yesterday's high AND today's low > yesterday's low."""
    try:
        if df is None or len(df) < 2:
            return None
        today = df.iloc[-1]
        yesterday = df.iloc[-2]
        t_high = safe_float(today["High"])
        t_low = safe_float(today["Low"])
        y_high = safe_float(yesterday["High"])
        y_low = safe_float(yesterday["Low"])
        if None in (t_high, t_low, y_high, y_low):
            return None
        if t_high >= y_high or t_low <= y_low:
            return None
        current_price = safe_float(today["Close"])
        return _make_signal(
            symbol=symbol, signal_type=INSIDE_DAY, direction="neutral",
            strength=4,
            reason=f"Inside day: range ₹{t_low:.2f}-₹{t_high:.2f} within yesterday's ₹{y_low:.2f}-₹{y_high:.2f}",
            risk="Compression pattern — breakout direction determines trade. Wait for next candle.",
            current_price=current_price,
            metadata={"today_high": t_high, "today_low": t_low,
                       "yesterday_high": y_high, "yesterday_low": y_low},
        )
    except Exception as e:
        logger.warning(f"detect_inside_day error for {symbol}: {e}")
        return None


def detect_bullish_engulfing(
    symbol: str, df: pd.DataFrame,
) -> Optional[dict]:
    """Yesterday bearish, today bullish and completely engulfs yesterday's body."""
    try:
        if df is None or len(df) < 2:
            return None
        today = df.iloc[-1]
        yesterday = df.iloc[-2]
        t_open = safe_float(today["Open"])
        t_close = safe_float(today["Close"])
        y_open = safe_float(yesterday["Open"])
        y_close = safe_float(yesterday["Close"])
        if None in (t_open, t_close, y_open, y_close):
            return None
        if y_close >= y_open:
            return None  # yesterday not bearish
        if t_close <= t_open:
            return None  # today not bullish
        if t_open > y_close or t_close < y_open:
            return None  # doesn't engulf
        current_price = t_close
        return _make_signal(
            symbol=symbol, signal_type=BULLISH_ENGULFING, direction="bullish",
            strength=7,
            reason=f"Bullish engulfing: today ₹{t_open:.2f}→₹{t_close:.2f} engulfs yesterday ₹{y_open:.2f}→₹{y_close:.2f}",
            risk="Most reliable at support or after a downtrend. Confirm with next session.",
            current_price=current_price,
            metadata={"today_open": t_open, "today_close": t_close,
                       "yesterday_open": y_open, "yesterday_close": y_close},
        )
    except Exception as e:
        logger.warning(f"detect_bullish_engulfing error for {symbol}: {e}")
        return None


def detect_bearish_engulfing(
    symbol: str, df: pd.DataFrame,
) -> Optional[dict]:
    """Yesterday bullish, today bearish and completely engulfs yesterday's body."""
    try:
        if df is None or len(df) < 2:
            return None
        today = df.iloc[-1]
        yesterday = df.iloc[-2]
        t_open = safe_float(today["Open"])
        t_close = safe_float(today["Close"])
        y_open = safe_float(yesterday["Open"])
        y_close = safe_float(yesterday["Close"])
        if None in (t_open, t_close, y_open, y_close):
            return None
        if y_close <= y_open:
            return None  # yesterday not bullish
        if t_close >= t_open:
            return None  # today not bearish
        if t_open < y_close or t_close > y_open:
            return None  # doesn't engulf
        current_price = t_close
        return _make_signal(
            symbol=symbol, signal_type=BEARISH_ENGULFING, direction="bearish",
            strength=7,
            reason=f"Bearish engulfing: today ₹{t_open:.2f}→₹{t_close:.2f} engulfs yesterday ₹{y_open:.2f}→₹{y_close:.2f}",
            risk="Most reliable at resistance or after an uptrend. Confirm with next session.",
            current_price=current_price,
            metadata={"today_open": t_open, "today_close": t_close,
                       "yesterday_open": y_open, "yesterday_close": y_close},
        )
    except Exception as e:
        logger.warning(f"detect_bearish_engulfing error for {symbol}: {e}")
        return None


def detect_morning_star(
    symbol: str, df: pd.DataFrame,
) -> Optional[dict]:
    """3-candle: big bearish, small body (doji-like), big bullish. Reversal."""
    try:
        if df is None or len(df) < 3:
            return None
        d1, d2, d3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
        o1, c1 = safe_float(d1["Open"]), safe_float(d1["Close"])
        o2, c2 = safe_float(d2["Open"]), safe_float(d2["Close"])
        o3, c3 = safe_float(d3["Open"]), safe_float(d3["Close"])
        if None in (o1, c1, o2, c2, o3, c3):
            return None
        body1 = abs(c1 - o1)
        body2 = abs(c2 - o2)
        body3 = abs(c3 - o3)
        if body1 == 0:
            return None
        # Day1 bearish, Day2 small body, Day3 bullish
        if c1 >= o1 or body2 > body1 * 0.3 or c3 <= o3:
            return None
        if body3 < body1 * 0.5:
            return None  # Day3 should be meaningfully bullish
        current_price = c3
        return _make_signal(
            symbol=symbol, signal_type=MORNING_STAR, direction="bullish",
            strength=7,
            reason=f"Morning star: bearish ₹{o1:.2f}→₹{c1:.2f}, doji, bullish ₹{o3:.2f}→₹{c3:.2f}",
            risk="Strong reversal pattern but requires trend context. Best after extended downtrend.",
            current_price=current_price,
            metadata={"day1_body": body1, "day2_body": body2, "day3_body": body3},
        )
    except Exception as e:
        logger.warning(f"detect_morning_star error for {symbol}: {e}")
        return None


def detect_evening_star(
    symbol: str, df: pd.DataFrame,
) -> Optional[dict]:
    """3-candle: big bullish, small body, big bearish. Bearish reversal."""
    try:
        if df is None or len(df) < 3:
            return None
        d1, d2, d3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
        o1, c1 = safe_float(d1["Open"]), safe_float(d1["Close"])
        o2, c2 = safe_float(d2["Open"]), safe_float(d2["Close"])
        o3, c3 = safe_float(d3["Open"]), safe_float(d3["Close"])
        if None in (o1, c1, o2, c2, o3, c3):
            return None
        body1 = abs(c1 - o1)
        body2 = abs(c2 - o2)
        body3 = abs(c3 - o3)
        if body1 == 0:
            return None
        # Day1 bullish, Day2 small body, Day3 bearish
        if c1 <= o1 or body2 > body1 * 0.3 or c3 >= o3:
            return None
        if body3 < body1 * 0.5:
            return None
        current_price = c3
        return _make_signal(
            symbol=symbol, signal_type=EVENING_STAR, direction="bearish",
            strength=7,
            reason=f"Evening star: bullish ₹{o1:.2f}→₹{c1:.2f}, doji, bearish ₹{o3:.2f}→₹{c3:.2f}",
            risk="Strong reversal pattern but requires trend context. Best after extended uptrend.",
            current_price=current_price,
            metadata={"day1_body": body1, "day2_body": body2, "day3_body": body3},
        )
    except Exception as e:
        logger.warning(f"detect_evening_star error for {symbol}: {e}")
        return None


def detect_hammer(
    symbol: str, df: pd.DataFrame,
) -> Optional[dict]:
    """Small body at top, long lower wick (2x+ body), at a low. Bullish reversal."""
    try:
        if df is None or len(df) < 10:
            return None
        today = df.iloc[-1]
        o = safe_float(today["Open"])
        c = safe_float(today["Close"])
        h = safe_float(today["High"])
        lo = safe_float(today["Low"])
        if None in (o, c, h, lo):
            return None
        body = abs(c - o)
        body_top = max(o, c)
        body_bottom = min(o, c)
        lower_wick = body_bottom - lo
        upper_wick = h - body_top
        if body == 0 or lower_wick < body * 2:
            return None
        if upper_wick > body * 0.5:
            return None  # upper wick should be small
        # Should be near recent lows
        recent_low = safe_float(df["Low"].iloc[-10:].min())
        if recent_low is None or lo > recent_low * 1.02:
            return None
        current_price = c
        return _make_signal(
            symbol=symbol, signal_type=HAMMER, direction="bullish",
            strength=6,
            reason=f"Hammer candle at ₹{c:.2f}: lower wick {lower_wick:.2f} vs body {body:.2f}",
            risk="Hammer is a reversal hint, not confirmation. Need bullish follow-through next session.",
            current_price=current_price,
            metadata={"body": body, "lower_wick": lower_wick, "upper_wick": upper_wick},
        )
    except Exception as e:
        logger.warning(f"detect_hammer error for {symbol}: {e}")
        return None


def detect_shooting_star(
    symbol: str, df: pd.DataFrame,
) -> Optional[dict]:
    """Small body at bottom, long upper wick (2x+ body), at a high. Bearish reversal."""
    try:
        if df is None or len(df) < 10:
            return None
        today = df.iloc[-1]
        o = safe_float(today["Open"])
        c = safe_float(today["Close"])
        h = safe_float(today["High"])
        lo = safe_float(today["Low"])
        if None in (o, c, h, lo):
            return None
        body = abs(c - o)
        body_top = max(o, c)
        body_bottom = min(o, c)
        upper_wick = h - body_top
        lower_wick = body_bottom - lo
        if body == 0 or upper_wick < body * 2:
            return None
        if lower_wick > body * 0.5:
            return None
        # Should be near recent highs
        recent_high = safe_float(df["High"].iloc[-10:].max())
        if recent_high is None or h < recent_high * 0.98:
            return None
        current_price = c
        return _make_signal(
            symbol=symbol, signal_type=SHOOTING_STAR, direction="bearish",
            strength=6,
            reason=f"Shooting star at ₹{c:.2f}: upper wick {upper_wick:.2f} vs body {body:.2f}",
            risk="Shooting star hints at reversal. Confirm with bearish follow-through next session.",
            current_price=current_price,
            metadata={"body": body, "upper_wick": upper_wick, "lower_wick": lower_wick},
        )
    except Exception as e:
        logger.warning(f"detect_shooting_star error for {symbol}: {e}")
        return None


def detect_ema_crossover(
    symbol: str, df: pd.DataFrame,
) -> Optional[dict]:
    """20 EMA crosses above/below 50 EMA."""
    try:
        if df is None or len(df) < 52:
            return None
        ema20 = df["Close"].ewm(span=20, adjust=False).mean()
        ema50 = df["Close"].ewm(span=50, adjust=False).mean()
        curr_20 = safe_float(ema20.iloc[-1])
        prev_20 = safe_float(ema20.iloc[-2])
        curr_50 = safe_float(ema50.iloc[-1])
        prev_50 = safe_float(ema50.iloc[-2])
        if None in (curr_20, prev_20, curr_50, prev_50):
            return None
        current_price = safe_float(df["Close"].iloc[-1])
        # Bullish crossover
        if prev_20 <= prev_50 and curr_20 > curr_50:
            return _make_signal(
                symbol=symbol, signal_type=EMA_CROSSOVER, direction="bullish",
                strength=6,
                reason=f"EMA 20 ({curr_20:.2f}) crossed above EMA 50 ({curr_50:.2f}). Bullish momentum.",
                risk="EMA crossovers lag price. Confirm with volume and price action.",
                current_price=current_price,
                metadata={"ema20": curr_20, "ema50": curr_50},
            )
        # Bearish crossover
        if prev_20 >= prev_50 and curr_20 < curr_50:
            return _make_signal(
                symbol=symbol, signal_type=EMA_CROSSOVER, direction="bearish",
                strength=6,
                reason=f"EMA 20 ({curr_20:.2f}) crossed below EMA 50 ({curr_50:.2f}). Bearish momentum.",
                risk="EMA crossovers lag price. Confirm with volume and price action.",
                current_price=current_price,
                metadata={"ema20": curr_20, "ema50": curr_50},
            )
        return None
    except Exception as e:
        logger.warning(f"detect_ema_crossover error for {symbol}: {e}")
        return None


def detect_52_week_high(
    symbol: str, df: pd.DataFrame,
) -> Optional[dict]:
    """Within 2% of 52-week high. Momentum signal."""
    try:
        if df is None or len(df) < 200:
            return None
        year_data = df.tail(252)
        week52_high = safe_float(year_data["High"].max())
        current_price = safe_float(df["Close"].iloc[-1])
        if week52_high is None or current_price is None or week52_high == 0:
            return None
        pct_from_high = (week52_high - current_price) / week52_high * 100
        if pct_from_high > 2.0:
            return None
        strength = 7 if pct_from_high < 0.5 else 6
        return _make_signal(
            symbol=symbol, signal_type=FIFTY_TWO_WEEK_HIGH, direction="bullish",
            strength=strength,
            reason=f"Within {pct_from_high:.1f}% of 52-week high (₹{week52_high:.2f}). Strong momentum.",
            risk="Stocks near 52-week highs can extend further or reverse sharply. Use trailing stops.",
            current_price=current_price,
            metadata={"week52_high": week52_high, "pct_from_high": round(pct_from_high, 2)},
        )
    except Exception as e:
        logger.warning(f"detect_52_week_high error for {symbol}: {e}")
        return None


def detect_52_week_low(
    symbol: str, df: pd.DataFrame,
) -> Optional[dict]:
    """Within 2% of 52-week low. Value signal."""
    try:
        if df is None or len(df) < 200:
            return None
        year_data = df.tail(252)
        week52_low = safe_float(year_data["Low"].min())
        current_price = safe_float(df["Close"].iloc[-1])
        if week52_low is None or current_price is None or week52_low == 0:
            return None
        pct_from_low = (current_price - week52_low) / week52_low * 100
        if pct_from_low > 2.0:
            return None
        strength = 6
        return _make_signal(
            symbol=symbol, signal_type=FIFTY_TWO_WEEK_LOW, direction="bearish",
            strength=strength,
            reason=f"Within {pct_from_low:.1f}% of 52-week low (₹{week52_low:.2f}). Potential value or further decline.",
            risk="Falling knives are dangerous. Look for volume dry-up and reversal candles before buying.",
            current_price=current_price,
            metadata={"week52_low": week52_low, "pct_from_low": round(pct_from_low, 2)},
        )
    except Exception as e:
        logger.warning(f"detect_52_week_low error for {symbol}: {e}")
        return None


def detect_gap_up(
    symbol: str, df: pd.DataFrame, threshold_pct: float = 2.0,
) -> Optional[dict]:
    """Today's open > yesterday's high by threshold%."""
    try:
        if df is None or len(df) < 2:
            return None
        today_open = safe_float(df["Open"].iloc[-1])
        yesterday_high = safe_float(df["High"].iloc[-2])
        if today_open is None or yesterday_high is None or yesterday_high == 0:
            return None
        gap_pct = (today_open - yesterday_high) / yesterday_high * 100
        if gap_pct < threshold_pct:
            return None
        current_price = safe_float(df["Close"].iloc[-1])
        strength = min(9, 5 + int(gap_pct))
        return _make_signal(
            symbol=symbol, signal_type=GAP_UP, direction="bullish",
            strength=strength,
            reason=f"Gap up {gap_pct:.1f}%: opened ₹{today_open:.2f} vs yesterday high ₹{yesterday_high:.2f}",
            risk="Gaps can fill quickly. Watch if price holds above gap level.",
            current_price=current_price,
            metadata={"gap_pct": round(gap_pct, 2), "today_open": today_open,
                       "yesterday_high": yesterday_high},
        )
    except Exception as e:
        logger.warning(f"detect_gap_up error for {symbol}: {e}")
        return None


def detect_gap_down(
    symbol: str, df: pd.DataFrame, threshold_pct: float = 2.0,
) -> Optional[dict]:
    """Today's open < yesterday's low by threshold%."""
    try:
        if df is None or len(df) < 2:
            return None
        today_open = safe_float(df["Open"].iloc[-1])
        yesterday_low = safe_float(df["Low"].iloc[-2])
        if today_open is None or yesterday_low is None or yesterday_low == 0:
            return None
        gap_pct = (yesterday_low - today_open) / yesterday_low * 100
        if gap_pct < threshold_pct:
            return None
        current_price = safe_float(df["Close"].iloc[-1])
        strength = min(9, 5 + int(gap_pct))
        return _make_signal(
            symbol=symbol, signal_type=GAP_DOWN, direction="bearish",
            strength=strength,
            reason=f"Gap down {gap_pct:.1f}%: opened ₹{today_open:.2f} vs yesterday low ₹{yesterday_low:.2f}",
            risk="Gap downs can be panic-driven. Watch for gap-fill recovery or continuation.",
            current_price=current_price,
            metadata={"gap_pct": round(gap_pct, 2), "today_open": today_open,
                       "yesterday_low": yesterday_low},
        )
    except Exception as e:
        logger.warning(f"detect_gap_down error for {symbol}: {e}")
        return None


def detect_volume_dry_up(
    symbol: str, df: pd.DataFrame, lookback: int = 10,
) -> Optional[dict]:
    """Volume declining to below 50% of average. Often precedes breakout."""
    try:
        if df is None or len(df) < lookback + 20:
            return None
        avg_vol = safe_float(df["Volume"].iloc[-(lookback + 20):-lookback].mean())
        recent_vol = safe_float(df["Volume"].iloc[-lookback:].mean())
        today_vol = safe_float(df["Volume"].iloc[-1])
        if avg_vol is None or recent_vol is None or today_vol is None or avg_vol == 0:
            return None
        ratio = recent_vol / avg_vol
        if ratio > 0.5:
            return None
        # Check if volume is declining (trending down)
        vols = df["Volume"].iloc[-lookback:].values
        declining = all(
            safe_float(vols[i]) is not None
            and safe_float(vols[i + 1]) is not None
            and vols[i] >= vols[i + 1]
            for i in range(0, min(3, len(vols) - 1))
        )
        current_price = safe_float(df["Close"].iloc[-1])
        strength = 5
        return _make_signal(
            symbol=symbol, signal_type=VOLUME_DRY_UP, direction="neutral",
            strength=strength,
            reason=f"Volume dry-up: recent avg {int(recent_vol):,} is {ratio:.0%} of 20-day avg {int(avg_vol):,}",
            risk="Low volume signals indecision. Watch for volume surge to indicate direction.",
            current_price=current_price,
            metadata={"avg_vol": avg_vol, "recent_vol": recent_vol,
                       "ratio": round(ratio, 2), "declining": declining},
        )
    except Exception as e:
        logger.warning(f"detect_volume_dry_up error for {symbol}: {e}")
        return None


# ---------------------------------------------------------------------------
# Master scanner
# ---------------------------------------------------------------------------

ALL_PATTERN_DETECTORS = [
    detect_double_bottom,
    detect_double_top,
    detect_head_and_shoulders,
    detect_inverse_head_and_shoulders,
    detect_cup_and_handle,
    detect_narrow_range,
    detect_consolidation_breakout,
    detect_inside_day,
    detect_bullish_engulfing,
    detect_bearish_engulfing,
    detect_morning_star,
    detect_evening_star,
    detect_hammer,
    detect_shooting_star,
    detect_ema_crossover,
    detect_52_week_high,
    detect_52_week_low,
    detect_gap_up,
    detect_gap_down,
    detect_volume_dry_up,
]


def scan_patterns(symbol: str, df: pd.DataFrame) -> list[dict[str, Any]]:
    """Run ALL pattern detectors on a symbol's OHLCV DataFrame.  Returns list of signals."""
    signals: list[dict[str, Any]] = []
    if df is None or df.empty:
        return signals
    for detector in ALL_PATTERN_DETECTORS:
        try:
            sig = detector(symbol, df)
            if sig is not None:
                signals.append(sig)
        except Exception as e:
            logger.warning(f"Pattern detector {detector.__name__} failed for {symbol}: {e}")
    return signals

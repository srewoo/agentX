from __future__ import annotations
"""
Technical analysis computations using the ``ta`` library (bukosabino/ta).

Provides RSI, MACD, ADX, Bollinger Bands, SMA/EMA, VWAP, Stochastic,
OBV, ATR, Ichimoku, CCI, Williams %R, MFI, plus support/resistance,
Fibonacci retracement, and Volume-Profile POC helpers.
"""
import logging
from typing import Any, Optional

import numpy as np
import pandas as pd
import ta

from app.utils import safe_float

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _last(series: pd.Series) -> float | None:
    """Return the last non-NaN value as a safe_float, or None."""
    if series is None or series.empty:
        return None
    val = series.iloc[-1]
    if pd.isna(val):
        return None
    return safe_float(val)


def _prev(series: pd.Series) -> float | None:
    """Return the second-to-last value as a safe_float, or None."""
    if series is None or len(series) < 2:
        return None
    val = series.iloc[-2]
    if pd.isna(val):
        return None
    return safe_float(val)


# ---------------------------------------------------------------------------
# Main technicals
# ---------------------------------------------------------------------------

def compute_technicals(df: pd.DataFrame) -> dict[str, Any]:
    """Compute a comprehensive set of technical indicators from a price DataFrame.

    Expects columns: Close, High, Low, Open, Volume.
    """
    if df.empty or len(df) < 20:
        return {}

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    open_ = df["Open"] if "Open" in df.columns else close
    volume = df["Volume"] if "Volume" in df.columns else pd.Series(np.zeros(len(df)), index=df.index)

    result: dict[str, Any] = {}

    current_price = safe_float(close.iloc[-1])
    result["current_price"] = current_price
    result["prev_price"] = safe_float(close.iloc[-2]) if len(close) > 1 else None

    # --- RSI (14) ---------------------------------------------------------
    try:
        rsi_ind = ta.momentum.RSIIndicator(close=close, window=14)
        rsi_series = rsi_ind.rsi()
        rsi_val = _last(rsi_series)
        result["rsi"] = rsi_val
        result["rsi_signal"] = (
            "Overbought" if rsi_val and rsi_val > 70
            else ("Oversold" if rsi_val and rsi_val < 30 else "Neutral")
        )
        result["rsi_prev"] = _prev(rsi_series)
    except Exception:
        logger.exception("RSI computation failed")
        result["rsi"] = None
        result["rsi_signal"] = "Neutral"
        result["rsi_prev"] = None

    # --- MACD (12, 26, 9) -------------------------------------------------
    try:
        macd_ind = ta.trend.MACD(close=close, window_slow=26, window_fast=12, window_sign=9)
        macd_line = macd_ind.macd()
        signal_line = macd_ind.macd_signal()
        macd_hist = macd_ind.macd_diff()

        macd_line_val = _last(macd_line)
        signal_line_val = _last(signal_line)

        result["macd"] = {
            "macd_line": macd_line_val,
            "macd_line_prev": _prev(macd_line),
            "signal_line": signal_line_val,
            "signal_line_prev": _prev(signal_line),
            "histogram": _last(macd_hist),
            "signal": (
                "Bullish" if macd_line_val is not None and signal_line_val is not None and macd_line_val > signal_line_val
                else "Bearish"
            ),
        }
    except Exception:
        logger.exception("MACD computation failed")
        result["macd"] = {
            "macd_line": None, "macd_line_prev": None,
            "signal_line": None, "signal_line_prev": None,
            "histogram": None, "signal": "Bearish",
        }

    # --- ADX (14) ---------------------------------------------------------
    try:
        adx_ind = ta.trend.ADXIndicator(high=high, low=low, close=close, window=14)
        result["adx"] = _last(adx_ind.adx())
    except Exception:
        logger.exception("ADX computation failed")
        result["adx"] = None

    # --- Moving Averages --------------------------------------------------
    try:
        sma20 = ta.trend.SMAIndicator(close=close, window=20).sma_indicator()
        sma50_val = None
        sma200_val = None
        if len(close) >= 50:
            sma50_val = _last(ta.trend.SMAIndicator(close=close, window=50).sma_indicator())
        if len(close) >= 200:
            sma200_val = _last(ta.trend.SMAIndicator(close=close, window=200).sma_indicator())
        ema20 = ta.trend.EMAIndicator(close=close, window=20).ema_indicator()

        sma20_val = _last(sma20)
        result["moving_averages"] = {
            "sma20": sma20_val,
            "sma50": sma50_val,
            "sma200": sma200_val,
            "ema20": _last(ema20),
        }
        result["price_vs_sma20"] = (
            "Above" if current_price and sma20_val and current_price > sma20_val else "Below"
        )
    except Exception:
        logger.exception("Moving averages computation failed")
        result["moving_averages"] = {"sma20": None, "sma50": None, "sma200": None, "ema20": None}
        result["price_vs_sma20"] = "Below"

    # --- Bollinger Bands (20, 2) ------------------------------------------
    try:
        bb_ind = ta.volatility.BollingerBands(close=close, window=20, window_dev=2)
        bb_upper_val = _last(bb_ind.bollinger_hband())
        bb_lower_val = _last(bb_ind.bollinger_lband())
        bb_middle_val = _last(bb_ind.bollinger_mavg())

        result["bollinger_bands"] = {
            "upper": bb_upper_val,
            "middle": bb_middle_val,
            "lower": bb_lower_val,
            "signal": (
                "Overbought" if current_price and bb_upper_val and current_price > bb_upper_val
                else ("Oversold" if current_price and bb_lower_val and current_price < bb_lower_val
                      else "Normal")
            ),
        }
    except Exception:
        logger.exception("Bollinger Bands computation failed")
        result["bollinger_bands"] = {"upper": None, "middle": None, "lower": None, "signal": "Normal"}

    # --- Volume -----------------------------------------------------------
    try:
        vol_avg_series = volume.rolling(window=20).mean()
        result["volume_avg_20"] = _last(vol_avg_series)
        result["volume_current"] = safe_float(volume.iloc[-1])
        result["volume_prev"] = safe_float(volume.iloc[-2]) if len(volume) > 1 else None
    except Exception:
        logger.exception("Volume computation failed")
        result["volume_avg_20"] = None
        result["volume_current"] = None
        result["volume_prev"] = None

    # --- VWAP -------------------------------------------------------------
    try:
        vwap_ind = ta.volume.VolumeWeightedAveragePrice(
            high=high, low=low, close=close, volume=volume,
        )
        result["vwap"] = _last(vwap_ind.volume_weighted_average_price())
    except Exception:
        logger.exception("VWAP computation failed")
        result["vwap"] = None

    # --- Stochastic Oscillator --------------------------------------------
    try:
        stoch_ind = ta.momentum.StochasticOscillator(
            high=high, low=low, close=close, window=14, smooth_window=3,
        )
        k_val = _last(stoch_ind.stoch())
        d_val = _last(stoch_ind.stoch_signal())

        if k_val is not None and d_val is not None:
            if k_val > d_val and k_val < 80:
                stoch_signal = "bullish"
            elif k_val < d_val and k_val > 20:
                stoch_signal = "bearish"
            else:
                stoch_signal = "neutral"
        else:
            stoch_signal = "neutral"

        result["stochastic"] = {"k": k_val, "d": d_val, "signal": stoch_signal}
    except Exception:
        logger.exception("Stochastic computation failed")
        result["stochastic"] = {"k": None, "d": None, "signal": "neutral"}

    # --- OBV (On-Balance Volume) ------------------------------------------
    try:
        obv_ind = ta.volume.OnBalanceVolumeIndicator(close=close, volume=volume)
        obv_series = obv_ind.on_balance_volume()
        obv_val = _last(obv_series)
        result["obv"] = obv_val

        # Determine OBV trend from last 5 values
        if len(obv_series) >= 5:
            obv_tail = obv_series.dropna().tail(5)
            if len(obv_tail) >= 3:
                slope = obv_tail.iloc[-1] - obv_tail.iloc[0]
                if slope > 0:
                    result["obv_trend"] = "rising"
                elif slope < 0:
                    result["obv_trend"] = "falling"
                else:
                    result["obv_trend"] = "flat"
            else:
                result["obv_trend"] = "flat"
        else:
            result["obv_trend"] = "flat"
    except Exception:
        logger.exception("OBV computation failed")
        result["obv"] = None
        result["obv_trend"] = "flat"

    # --- ATR (14) ---------------------------------------------------------
    try:
        atr_ind = ta.volatility.AverageTrueRange(high=high, low=low, close=close, window=14)
        atr_val = _last(atr_ind.average_true_range())
        result["atr"] = atr_val
        result["atr_pct"] = (
            round(atr_val / current_price * 100, 4)
            if atr_val is not None and current_price
            else None
        )
    except Exception:
        logger.exception("ATR computation failed")
        result["atr"] = None
        result["atr_pct"] = None

    # --- Ichimoku ---------------------------------------------------------
    try:
        ichi_ind = ta.trend.IchimokuIndicator(high=high, low=low, window1=9, window2=26, window3=52)
        tenkan = _last(ichi_ind.ichimoku_conversion_line())
        kijun = _last(ichi_ind.ichimoku_base_line())
        senkou_a = _last(ichi_ind.ichimoku_a())
        senkou_b = _last(ichi_ind.ichimoku_b())

        if tenkan is not None and kijun is not None and senkou_a is not None and senkou_b is not None:
            if tenkan > kijun and current_price and current_price > max(senkou_a, senkou_b):
                ichi_signal = "bullish"
            elif tenkan < kijun and current_price and current_price < min(senkou_a, senkou_b):
                ichi_signal = "bearish"
            else:
                ichi_signal = "neutral"
        else:
            ichi_signal = "neutral"

        result["ichimoku"] = {
            "tenkan": tenkan,
            "kijun": kijun,
            "senkou_a": senkou_a,
            "senkou_b": senkou_b,
            "signal": ichi_signal,
        }
    except Exception:
        logger.exception("Ichimoku computation failed")
        result["ichimoku"] = {
            "tenkan": None, "kijun": None,
            "senkou_a": None, "senkou_b": None,
            "signal": "neutral",
        }

    # --- CCI (20) ---------------------------------------------------------
    try:
        cci_ind = ta.trend.CCIIndicator(high=high, low=low, close=close, window=20)
        result["cci"] = _last(cci_ind.cci())
    except Exception:
        logger.exception("CCI computation failed")
        result["cci"] = None

    # --- Williams %R ------------------------------------------------------
    try:
        wr_ind = ta.momentum.WilliamsRIndicator(high=high, low=low, close=close, lbp=14)
        result["williams_r"] = _last(wr_ind.williams_r())
    except Exception:
        logger.exception("Williams %R computation failed")
        result["williams_r"] = None

    # --- MFI (Money Flow Index) -------------------------------------------
    try:
        mfi_ind = ta.volume.MFIIndicator(high=high, low=low, close=close, volume=volume, window=14)
        result["mfi"] = _last(mfi_ind.money_flow_index())
    except Exception:
        logger.exception("MFI computation failed")
        result["mfi"] = None

    return result


# ---------------------------------------------------------------------------
# Support / Resistance, Fibonacci, Volume Profile (unchanged)
# ---------------------------------------------------------------------------

def compute_support_resistance(df: pd.DataFrame) -> dict[str, Any]:
    """Compute pivot-based support and resistance levels."""
    if df.empty or len(df) < 5:
        return {}

    close, high, low = df["Close"], df["High"], df["Low"]
    current_price = safe_float(close.iloc[-1])

    last_high = safe_float(high.iloc[-1])
    last_low = safe_float(low.iloc[-1])
    last_close = safe_float(close.iloc[-1])

    pivot = round((last_high + last_low + last_close) / 3, 2) if all([last_high, last_low, last_close]) else None

    r1 = round(2 * pivot - last_low, 2) if pivot and last_low else None
    r2 = round(pivot + (last_high - last_low), 2) if pivot and last_high and last_low else None
    r3 = round(last_high + 2 * (pivot - last_low), 2) if pivot and last_high and last_low else None
    s1 = round(2 * pivot - last_high, 2) if pivot and last_high else None
    s2 = round(pivot - (last_high - last_low), 2) if pivot and last_high and last_low else None
    s3 = round(last_low - 2 * (last_high - pivot), 2) if pivot and last_high and last_low else None

    return {
        "pivot": pivot,
        "resistance": {"r1": r1, "r2": r2, "r3": r3},
        "support": {"s1": s1, "s2": s2, "s3": s3},
        "period_highs_lows": {
            "high_52w": safe_float(high.max()),
            "low_52w": safe_float(low.min()),
            "high_6m": safe_float(high.tail(130).max()) if len(df) >= 130 else safe_float(high.max()),
            "low_6m": safe_float(low.tail(130).min()) if len(df) >= 130 else safe_float(low.min()),
            "high_1m": safe_float(high.tail(22).max()),
            "low_1m": safe_float(low.tail(22).min()),
        },
    }


def compute_fibonacci_levels(df: pd.DataFrame, period: int = 120) -> dict[str, Any]:
    """Calculate Fibonacci retracement levels over the given period."""
    if df.empty or len(df) < 5:
        return {}

    data = df.tail(min(len(df), period))
    swing_high = float(data["High"].max())
    swing_low = float(data["Low"].min())

    if pd.isna(swing_high) or pd.isna(swing_low) or swing_high == swing_low:
        return {}

    diff = swing_high - swing_low
    return {
        "swing_high": round(swing_high, 2),
        "swing_low": round(swing_low, 2),
        "levels": {
            "level_0": round(swing_high, 2),
            "level_23_6": round(swing_high - 0.236 * diff, 2),
            "level_38_2": round(swing_high - 0.382 * diff, 2),
            "level_50_0": round(swing_high - 0.5 * diff, 2),
            "level_61_8": round(swing_high - 0.618 * diff, 2),
            "level_78_6": round(swing_high - 0.786 * diff, 2),
            "level_100": round(swing_low, 2),
        },
    }


def detect_divergence(
    price_series: pd.Series,
    indicator_series: pd.Series,
    lookback: int = 20,
    pivot_bars: int = 5,
) -> dict[str, Any]:
    """Detect RSI/MACD divergences using swing-high/low pivots.

    Pivot detection: a bar is a swing high if it's the max over [i-pivot_bars, i+pivot_bars].
    A bar is a swing low if it's the min over the same window.

    Returns:
        {
            "bullish": bool,   # price lower low + indicator higher low → reversal up
            "bearish": bool,   # price higher high + indicator lower high → reversal down
            "type": str,       # "bullish", "bearish", or "none"
        }
    """
    result = {"bullish": False, "bearish": False, "type": "none"}

    if price_series is None or indicator_series is None:
        return result

    price = price_series.dropna()
    indicator = indicator_series.dropna()

    # Align on common index
    common_idx = price.index.intersection(indicator.index)
    if len(common_idx) < lookback * 2:
        return result

    price = price.loc[common_idx].tail(lookback + pivot_bars * 2)
    indicator = indicator.loc[common_idx].tail(lookback + pivot_bars * 2)

    n = len(price)
    if n < pivot_bars * 4:
        return result

    price_arr = price.values
    ind_arr = indicator.values

    # Find swing highs and lows (exclude last pivot_bars bars — incomplete pivots)
    scan_range = range(pivot_bars, n - pivot_bars)

    swing_lows_price: list[tuple[int, float]] = []
    swing_highs_price: list[tuple[int, float]] = []
    swing_lows_ind: list[tuple[int, float]] = []
    swing_highs_ind: list[tuple[int, float]] = []

    for i in scan_range:
        window_p = price_arr[i - pivot_bars: i + pivot_bars + 1]
        window_ind = ind_arr[i - pivot_bars: i + pivot_bars + 1]

        if price_arr[i] == window_p.min():
            swing_lows_price.append((i, price_arr[i]))
        if price_arr[i] == window_p.max():
            swing_highs_price.append((i, price_arr[i]))
        if ind_arr[i] == window_ind.min():
            swing_lows_ind.append((i, ind_arr[i]))
        if ind_arr[i] == window_ind.max():
            swing_highs_ind.append((i, ind_arr[i]))

    # Need at least 2 swing lows/highs on each side for divergence check
    if len(swing_lows_price) >= 2 and len(swing_lows_ind) >= 2:
        # Compare most recent two swing lows
        p_low1, p_low2 = swing_lows_price[-2], swing_lows_price[-1]
        # Find closest indicator swing low to each price swing low
        def closest_ind_low(bar_idx: int) -> Optional[tuple[int, float]]:
            candidates = [sl for sl in swing_lows_ind if abs(sl[0] - bar_idx) <= pivot_bars * 3]
            return min(candidates, key=lambda x: abs(x[0] - bar_idx)) if candidates else None

        i1 = closest_ind_low(p_low1[0])
        i2 = closest_ind_low(p_low2[0])

        if i1 and i2 and p_low2[0] > p_low1[0]:
            # Bullish divergence: price lower low, indicator higher low
            if p_low2[1] < p_low1[1] and i2[1] > i1[1]:
                result["bullish"] = True
                result["type"] = "bullish"

    if len(swing_highs_price) >= 2 and len(swing_highs_ind) >= 2:
        p_high1, p_high2 = swing_highs_price[-2], swing_highs_price[-1]

        def closest_ind_high(bar_idx: int) -> Optional[tuple[int, float]]:
            candidates = [sh for sh in swing_highs_ind if abs(sh[0] - bar_idx) <= pivot_bars * 3]
            return min(candidates, key=lambda x: abs(x[0] - bar_idx)) if candidates else None

        i1 = closest_ind_high(p_high1[0])
        i2 = closest_ind_high(p_high2[0])

        if i1 and i2 and p_high2[0] > p_high1[0]:
            # Bearish divergence: price higher high, indicator lower high
            if p_high2[1] > p_high1[1] and i2[1] < i1[1]:
                result["bearish"] = True
                if not result["bullish"]:
                    result["type"] = "bearish"

    return result


def compute_volume_profile_poc(df: pd.DataFrame, bins: int = 20) -> Optional[float]:
    """Approximate Volume Profile Point of Control (highest volume price)."""
    if df.empty or "Volume" not in df.columns or len(df) < 10:
        return None

    valid = df.dropna(subset=["Close", "Volume"]).copy()
    if valid.empty:
        return None

    min_price = valid["Low"].min()
    max_price = valid["High"].max()
    if min_price == max_price or pd.isna(min_price) or pd.isna(max_price):
        return None

    price_bins = pd.cut(valid["Close"], bins=bins)
    volume_by_price = valid.groupby(price_bins, observed=False)["Volume"].sum()
    poc_bin = volume_by_price.idxmax()

    if pd.isna(poc_bin):
        return None
    return round(float(poc_bin.mid), 2)

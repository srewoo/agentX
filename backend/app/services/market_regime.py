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

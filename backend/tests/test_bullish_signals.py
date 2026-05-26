"""Tests for the new bullish detectors (PEAD + Quality Breakout)."""
from __future__ import annotations

import pandas as pd
import pytest

from app.services.bullish_signals import (
    PEAD,
    QUALITY_BREAKOUT,
    detect_pead,
    detect_quality_breakout,
)


def _earnings_day_df(gap_pct: float = 4.0, vol_multiple: float = 3.0) -> pd.DataFrame:
    """Construct a 25-bar series with a clean PEAD setup on the last bar."""
    prev_closes = [100.0 + i * 0.05 for i in range(24)]
    vols = [1_000_000] * 24
    df = pd.DataFrame({
        "Open": prev_closes,
        "High": [c * 1.005 for c in prev_closes],
        "Low": [c * 0.995 for c in prev_closes],
        "Close": prev_closes,
        "Volume": vols,
    })
    prev_close = df["Close"].iloc[-1]
    gap_open = prev_close * (1 + gap_pct / 100)
    bar = pd.DataFrame({
        "Open": [gap_open],
        "High": [gap_open * 1.02],
        "Low": [gap_open * 0.999],     # closes near high → upper half of range
        "Close": [gap_open * 1.015],
        "Volume": [int(vols[0] * vol_multiple)],
    })
    return pd.concat([df, bar], ignore_index=True)


# ── PEAD ──────────────────────────────────────────────────────────────────

def test_pead_fires_on_clean_setup():
    df = _earnings_day_df()
    sig = detect_pead("RELIANCE", df, {}, earnings_recent_days=1, delivery_pct=65.0)
    assert sig is not None
    assert sig["signal_type"] == PEAD
    assert sig["direction"] == "bullish"
    assert sig["strength"] >= 9


def test_pead_skips_when_no_earnings_context():
    df = _earnings_day_df()
    assert detect_pead("X", df, {}, earnings_recent_days=None, delivery_pct=70) is None


def test_pead_skips_when_earnings_stale():
    df = _earnings_day_df()
    assert detect_pead("X", df, {}, earnings_recent_days=10, delivery_pct=70) is None


def test_pead_skips_when_delivery_too_low():
    df = _earnings_day_df()
    assert detect_pead("X", df, {}, earnings_recent_days=1, delivery_pct=20) is None


def test_pead_skips_when_gap_too_small():
    df = _earnings_day_df(gap_pct=1.0)
    assert detect_pead("X", df, {}, earnings_recent_days=1, delivery_pct=70) is None


def test_pead_skips_when_volume_too_low():
    df = _earnings_day_df(vol_multiple=1.2)
    assert detect_pead("X", df, {}, earnings_recent_days=1, delivery_pct=70) is None


def test_pead_skips_when_bar_closes_in_lower_half():
    """Fade-the-gap bars (closes near low) shouldn't fire PEAD."""
    df = _earnings_day_df()
    # Force the last bar to close near the low → lower half of range.
    df.iloc[-1, df.columns.get_loc("Close")] = df["Low"].iloc[-1] * 1.001
    assert detect_pead("X", df, {}, earnings_recent_days=1, delivery_pct=70) is None


# ── Quality Breakout ──────────────────────────────────────────────────────

def _breakout_df(close_above_pct: float = 2.0, vol_multiple: float = 2.0) -> pd.DataFrame:
    """Bar series where the last close cleanly breaks the 20-day high."""
    base = 100.0
    rows = []
    for _ in range(22):
        rows.append({
            "Open": base, "High": base * 1.02, "Low": base * 0.99, "Close": base,
            "Volume": 1_000_000,
        })
    df = pd.DataFrame(rows)
    prior_20d_high = df["High"].iloc[-21:-1].max()
    new_close = prior_20d_high * (1 + close_above_pct / 100)
    breakout = pd.DataFrame([{
        "Open": prior_20d_high,
        "High": new_close * 1.005,
        "Low": prior_20d_high * 0.998,
        "Close": new_close,
        "Volume": int(1_000_000 * vol_multiple),
    }])
    return pd.concat([df, breakout], ignore_index=True)


def _ok_fundamentals() -> dict:
    return {
        "fundamental_score": 7,
        "valuation": {"pe": 18.0, "pb": 2.1},
        "profitability": {"roe": 0.16},
        "financial_health": {"debt_to_equity": 0.6},
        "growth": {"revenue_growth": 0.12, "earnings_growth": 0.20},
    }


def test_quality_breakout_fires_with_ok_fundamentals():
    df = _breakout_df()
    sig = detect_quality_breakout(
        "INFY", df, {},
        fundamentals=_ok_fundamentals(),
        delivery_pct=55.0,
    )
    assert sig is not None
    assert sig["signal_type"] == QUALITY_BREAKOUT
    assert sig["direction"] == "bullish"
    assert sig["strength"] >= 7


def test_quality_breakout_skips_without_fundamentals():
    df = _breakout_df()
    assert detect_quality_breakout(
        "X", df, {}, fundamentals=None, delivery_pct=55.0
    ) is None


def test_quality_breakout_skips_when_pe_too_high():
    df = _breakout_df()
    f = _ok_fundamentals()
    f["valuation"]["pe"] = 120.0
    assert detect_quality_breakout("X", df, {}, fundamentals=f, delivery_pct=55) is None


def test_quality_breakout_skips_when_roe_too_low():
    df = _breakout_df()
    f = _ok_fundamentals()
    f["profitability"]["roe"] = 0.05
    assert detect_quality_breakout("X", df, {}, fundamentals=f, delivery_pct=55) is None


def test_quality_breakout_skips_below_breakout_level():
    """Last close not above 20d high → no signal."""
    df = _breakout_df(close_above_pct=-1.0)
    assert detect_quality_breakout(
        "X", df, {}, fundamentals=_ok_fundamentals(), delivery_pct=55,
    ) is None


def test_quality_breakout_skips_low_delivery():
    df = _breakout_df()
    assert detect_quality_breakout(
        "X", df, {}, fundamentals=_ok_fundamentals(), delivery_pct=30,
    ) is None

"""
Per-signal-type historical edge, derived from the 49-stock NIFTY backtest run
of 2026-05-07 (1y period, 5d evaluation window, net of 0.20% transaction cost).

Sample size: 25,641 signals across 49 stocks over ~250 trading days.

Each entry holds {win_rate_pct, avg_pnl_pct, trades, family} for a
(signal_type, direction) tuple. The 'family' classification is used by the
confluence detector to enforce diversity in the stack — stacking two
correlated chart patterns is what made bullish confluence net-negative
in this run despite each component looking individually plausible.

Refresh: regenerate from `backtest_results/json_<latest>` whenever the signal
engine changes materially. This file is the single source of truth for the
extension's edge UX.
"""
from __future__ import annotations
from typing import Optional

# Family taxonomy — used by confluence diversity check.
#  - momentum:   trend / momentum (MACD, EMA cross, price spike)
#  - divergence: classical divergences (RSI, MACD divergence)
#  - pattern:    chart patterns (H&S, double-top/bottom, cup&handle, etc.)
#  - candle:     single/multi-candle reversal patterns
#  - volatility: gap / NR / inside-day setups
#  - meanrev:    overbought/oversold (RSI extreme)
#  - extreme:    52w highs/lows
#  - volume:     volume-driven signals (volume spike, dry-up)
SIGNAL_FAMILY: dict[str, str] = {
    "price_spike": "momentum",
    "macd_crossover": "momentum",
    "ema_crossover": "momentum",
    "breakout": "momentum",
    "consolidation_breakout": "momentum",
    "rsi_divergence": "divergence",
    "macd_divergence": "divergence",
    "double_bottom": "pattern",
    "double_top": "pattern",
    "head_and_shoulders": "pattern",
    "inverse_head_and_shoulders": "pattern",
    "cup_and_handle": "pattern",
    "bullish_engulfing": "candle",
    "bearish_engulfing": "candle",
    "morning_star": "candle",
    "evening_star": "candle",
    "hammer": "candle",
    "shooting_star": "candle",
    "gap_up": "volatility",
    "gap_down": "volatility",
    "narrow_range": "volatility",
    "inside_day": "volatility",
    "rsi_extreme": "meanrev",
    "52_week_high": "extreme",
    "52_week_low": "extreme",
    "volume_spike": "volume",
    "volume_dry_up": "volume",
    "sentiment_shift": "sentiment",
    "options_flow": "flow",
}

# Edge data — keyed by (signal_type, direction). Bearish/bullish only;
# neutral excluded from the table because they're unrateable in directional terms.
SIGNAL_EDGE: dict[tuple[str, str], dict[str, float]] = {
    # --- WINNING SETUPS ---
    ("gap_down", "bearish"):                 {"win_rate": 65.3, "avg_pnl": 2.18, "trades": 95},
    ("gap_up", "bullish"):                   {"win_rate": 64.0, "avg_pnl": 1.43, "trades": 100},
    ("rsi_divergence", "bearish"):           {"win_rate": 54.6, "avg_pnl": 1.01, "trades": 582},
    ("macd_divergence", "bearish"):          {"win_rate": 52.0, "avg_pnl": 0.86, "trades": 475},
    ("macd_divergence", "bullish"):          {"win_rate": 57.1, "avg_pnl": 0.47, "trades": 347},
    ("price_spike", "bullish"):              {"win_rate": 53.1, "avg_pnl": 0.47, "trades": 294},
    ("ema_crossover", "bearish"):            {"win_rate": 55.7, "avg_pnl": 0.29, "trades": 106},
    ("macd_crossover", "bullish"):           {"win_rate": 50.3, "avg_pnl": 0.26, "trades": 396},
    ("rsi_divergence", "bullish"):           {"win_rate": 54.0, "avg_pnl": 0.25, "trades": 465},
    ("confluence", "bearish"):               {"win_rate": 48.6, "avg_pnl": 0.19, "trades": 1905},
    ("rsi_extreme", "bearish"):              {"win_rate": 50.1, "avg_pnl": 0.04, "trades": 517},

    # --- BREAK-EVEN ---
    ("macd_crossover", "bearish"):           {"win_rate": 51.2, "avg_pnl": -0.01, "trades": 369},
    ("ema_crossover", "bullish"):            {"win_rate": 47.6, "avg_pnl": -0.02, "trades": 84},
    ("double_top", "bearish"):               {"win_rate": 45.3, "avg_pnl": -0.06, "trades": 3078},
    ("evening_star", "bearish"):             {"win_rate": 49.8, "avg_pnl": -0.07, "trades": 263},
    ("bearish_engulfing", "bearish"):        {"win_rate": 45.5, "avg_pnl": -0.07, "trades": 596},
    ("double_bottom", "bullish"):            {"win_rate": 49.2, "avg_pnl": -0.13, "trades": 2601},

    # --- LOSING SETUPS ---
    ("bullish_engulfing", "bullish"):        {"win_rate": 49.0, "avg_pnl": -0.26, "trades": 461},
    ("confluence", "bullish"):               {"win_rate": 47.5, "avg_pnl": -0.30, "trades": 1787},
    ("52_week_low", "bearish"):              {"win_rate": 51.6, "avg_pnl": -0.30, "trades": 217},
    ("morning_star", "bullish"):             {"win_rate": 40.0, "avg_pnl": -0.37, "trades": 270},
    ("rsi_extreme", "bullish"):              {"win_rate": 43.4, "avg_pnl": -0.41, "trades": 722},
    ("inverse_head_and_shoulders", "bullish"): {"win_rate": 46.8, "avg_pnl": -0.47, "trades": 1081},
    ("price_spike", "bearish"):              {"win_rate": 45.7, "avg_pnl": -0.51, "trades": 291},
    ("shooting_star", "bearish"):            {"win_rate": 50.9, "avg_pnl": -0.56, "trades": 106},
    ("head_and_shoulders", "bearish"):       {"win_rate": 42.6, "avg_pnl": -0.68, "trades": 1311},
    ("cup_and_handle", "bullish"):           {"win_rate": 40.1, "avg_pnl": -0.99, "trades": 1181},
    ("hammer", "bullish"):                   {"win_rate": 43.2, "avg_pnl": -1.50, "trades": 111},
    ("52_week_high", "bullish"):             {"win_rate": 28.4, "avg_pnl": -1.84, "trades": 109},
}

# Signal types whose default expectancy is negative AND the trade volume is large
# enough that we recommend muting by default. Surfaced to the extension as a
# "recommended mutes" suggestion at first run.
RECOMMENDED_MUTES: list[str] = [
    "52_week_high",
    "cup_and_handle",
    "head_and_shoulders",
    "inverse_head_and_shoulders",
    "hammer",
]

# Methodology metadata so the UI can disclose what it's looking at.
EDGE_META = {
    "source": "internal_backtest_2026-05-07",
    "period": "1y",
    "eval_window_days": 5,
    "stocks": 49,
    "total_signals": 25641,
    "transaction_cost_pct": 0.20,
}


def get_edge(signal_type: str, direction: str) -> Optional[dict]:
    """Look up edge for a (signal_type, direction) pair. Returns None if unknown."""
    return SIGNAL_EDGE.get((signal_type, direction))


def get_family(signal_type: str) -> str:
    """Return the family of a signal type ('momentum', 'pattern', etc.)."""
    return SIGNAL_FAMILY.get(signal_type, "other")


def all_edge_rows() -> list[dict]:
    """Return the full edge table as a sorted list (for the API endpoint)."""
    rows = []
    for (stype, direction), data in SIGNAL_EDGE.items():
        rows.append({
            "signal_type": stype,
            "direction": direction,
            "family": get_family(stype),
            **data,
        })
    rows.sort(key=lambda r: r["avg_pnl"], reverse=True)
    return rows

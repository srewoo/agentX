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
import logging
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

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

# Backtest-driven execution policy. These setups are allowed as information,
# but should not become trade/paper-trade candidates unless other independent
# evidence confirms them.
HARD_CONFIRMATION_REQUIRED: set[tuple[str, str]] = {
    ("cup_and_handle", "bullish"),
    ("hammer", "bullish"),
    ("head_and_shoulders", "bearish"),
}

SOFT_CONFIRMATION_REQUIRED: set[tuple[str, str]] = {
    ("rsi_extreme", "bullish"),
    ("bearish_engulfing", "bearish"),
    ("double_bottom", "bullish"),
    ("confluence", "bullish"),
}

WEAK_DIRECTIONAL_SETUPS = HARD_CONFIRMATION_REQUIRED | SOFT_CONFIRMATION_REQUIRED

# Methodology metadata so the UI can disclose what it's looking at.
EDGE_META = {
    "source": "internal_backtest_2026-05-07",
    "period": "1y",
    "eval_window_days": 5,
    "stocks": 49,
    "total_signals": 25641,
    "transaction_cost_pct": 0.20,
}


# ── Live override cache ──────────────────────────────────────────────────
# The weekly autonomous backtest writes per-key edge into the
# `signal_edge_overrides` SQLite table. `seed_edge_overrides()` (called at
# app startup) loads them into this dict; `get_edge` prefers an override
# when present so the displayed edge tracks real recent performance instead
# of staying frozen at the 2026-05-07 baseline.
#
# Guardrails (enforced at write time, not here): an override is only stored
# when the latest weekly backtest has >= _OVERRIDE_MIN_TRADES samples for
# that (signal_type, direction). The static SIGNAL_EDGE table remains the
# cold-start fallback so a sparse backtest never erases known edges.
_OVERRIDE_MIN_TRADES = 30
_edge_overrides: dict[tuple[str, str], dict] = {}


def set_edge_overrides(rows: dict[tuple[str, str], dict]) -> None:
    """Replace the in-memory override map. Called by the weekly backtest."""
    global _edge_overrides
    _edge_overrides = dict(rows)


async def seed_edge_overrides() -> int:
    """Load overrides from SQLite at startup. Returns number loaded."""
    from app.database import DB_PATH
    loaded: dict[tuple[str, str], dict] = {}
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT signal_type, direction, win_rate, avg_pnl, trades "
                "FROM signal_edge_overrides"
            ) as cur:
                async for row in cur:
                    loaded[(row["signal_type"], row["direction"])] = {
                        "win_rate": float(row["win_rate"]),
                        "avg_pnl": float(row["avg_pnl"]),
                        "trades": int(row["trades"]),
                    }
    except Exception as e:
        logger.debug("seed_edge_overrides: %s", e)
    set_edge_overrides(loaded)
    return len(loaded)


async def write_edge_overrides(
    rows: dict[tuple[str, str], dict],
    min_trades: int = _OVERRIDE_MIN_TRADES,
) -> int:
    """Persist + activate per-key overrides. Returns number written.

    Skips rows below `min_trades` so a thin weekly run can never erase
    a known-good baseline edge. Replaces all rows in one transaction so
    keys that no longer meet the threshold are removed cleanly.
    """
    from app.database import DB_PATH
    keep: dict[tuple[str, str], dict] = {
        k: v for k, v in rows.items() if (v.get("trades") or 0) >= min_trades
    }
    now = datetime.now(timezone.utc).isoformat()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM signal_edge_overrides")
            for (stype, direction), d in keep.items():
                await db.execute(
                    "INSERT INTO signal_edge_overrides "
                    "(signal_type, direction, win_rate, avg_pnl, trades, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (stype, direction, float(d["win_rate"]), float(d["avg_pnl"]),
                     int(d["trades"]), now),
                )
            await db.commit()
    except Exception as e:
        logger.warning("write_edge_overrides failed (non-critical): %s", e)
        return 0
    set_edge_overrides(keep)
    logger.info(
        "signal_edge overrides refreshed: %d/%d keys persisted (min_trades=%d)",
        len(keep), len(rows), min_trades,
    )
    return len(keep)


def get_edge(signal_type: str, direction: str) -> Optional[dict]:
    """Look up edge for a (signal_type, direction) pair. Returns None if unknown.

    Live overrides take priority over the cold-start `SIGNAL_EDGE` table so
    the UI reflects the latest autonomous backtest run.
    """
    key = (signal_type, direction)
    return _edge_overrides.get(key) or SIGNAL_EDGE.get(key)


def has_positive_edge(signal_type: str, direction: str, min_trades: int = 50) -> bool:
    """True when the cold-start backtest says the setup has positive expectancy."""
    edge = get_edge(signal_type, direction)
    if not edge:
        return False
    return edge.get("trades", 0) >= min_trades and edge.get("avg_pnl", 0.0) > 0


def requires_confirmation(signal_type: str, direction: str) -> str | None:
    """Return the required policy for a weak setup: hard, soft, or None."""
    key = (signal_type, direction)
    if key in HARD_CONFIRMATION_REQUIRED:
        return "hard"
    if key in SOFT_CONFIRMATION_REQUIRED:
        return "soft"
    return None


def get_family(signal_type: str) -> str:
    """Return the family of a signal type ('momentum', 'pattern', etc.)."""
    return SIGNAL_FAMILY.get(signal_type, "other")


def all_edge_rows() -> list[dict]:
    """Return the full edge table as a sorted list (for the API endpoint).

    Overrides shadow the cold-start values per key, so the response always
    reflects the most-recent autonomous backtest where data is available.
    """
    rows = []
    seen: set[tuple[str, str]] = set()
    for key, data in _edge_overrides.items():
        stype, direction = key
        seen.add(key)
        rows.append({
            "signal_type": stype,
            "direction": direction,
            "family": get_family(stype),
            "source": "live",
            **data,
        })
    for (stype, direction), data in SIGNAL_EDGE.items():
        if (stype, direction) in seen:
            continue
        rows.append({
            "signal_type": stype,
            "direction": direction,
            "family": get_family(stype),
            "source": "baseline",
            **data,
        })
    rows.sort(key=lambda r: r["avg_pnl"], reverse=True)
    return rows

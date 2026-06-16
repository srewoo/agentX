"""
Per-signal-type historical edge, derived from the 2026-05-21 walk-forward
out-of-sample run (40-stock NIFTY universe, 2y period, 4 expanding-window
folds, 5d evaluation window, net of 0.20% transaction cost).

Sample size: **37,403 OOS trades** across 40 stocks — every win-rate below is
from periods the signal engine never saw during fold-training, so the table
is honest about what these setups actually deliver. `win_rate_lb95` is the
Wilson 95% lower bound — the conservative win rate you should plan around.

Each entry holds {win_rate_pct, avg_pnl_pct, trades, win_rate_lb95, family}
for a (signal_type, direction) tuple. The 'family' classification is used
by the confluence detector to enforce diversity in the stack.

Refresh: rerun `POST /api/backtest/walk-forward?limit=40&n_folds=4&period=2y`
whenever the signal engine changes materially. This file is the single
source of truth for the extension's edge UX.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

# Date the static SIGNAL_EDGE baseline below was generated from (the
# 2026-05-21 walk-forward OOS run). This is the cold-start prior; live
# overrides from the weekly backtest shadow it per key. Surfaced via
# `edge_freshness()` so the API/UI can warn when the priors are stale and
# the engine is flying on numbers that may no longer hold.
EDGE_BASELINE_DATE = "2026-05-21"

# Beyond this many days without a live override refresh, the edge table is
# considered stale — the signal engine may have drifted from these priors.
EDGE_STALE_AFTER_DAYS = 45

# Timestamp (ISO-8601) of the most recent live override refresh, learned from
# the `signal_edge_overrides.updated_at` column at seed/write time. None when
# no override has ever been written (running purely on the static baseline).
_edge_last_refreshed_at: Optional[str] = None

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
# neutral excluded because they're unrateable in directional terms.
# Sorted by win_rate_lb95 descending — the conservative ranking that
# protects against small-sample optimism.
SIGNAL_EDGE: dict[tuple[str, str], dict[str, float]] = {
    # --- POSITIVE OOS EDGE (Wilson LB ≥ 49%, positive avg_pnl) ---
    ("gap_up", "bullish"):                   {"win_rate": 61.9, "avg_pnl": 1.128, "trades": 126, "win_rate_lb95": 53.19},
    ("rsi_extreme", "bearish"):              {"win_rate": 53.35, "avg_pnl": 0.34, "trades": 836, "win_rate_lb95": 49.96},
    ("macd_divergence", "bullish"):          {"win_rate": 53.57, "avg_pnl": 0.489, "trades": 715, "win_rate_lb95": 49.9},
    ("evening_star", "bearish"):             {"win_rate": 54.05, "avg_pnl": 0.275, "trades": 444, "win_rate_lb95": 49.4},

    # --- BREAK-EVEN AFTER COSTS ---
    ("rsi_divergence", "bullish"):           {"win_rate": 51.0, "avg_pnl": 0.144, "trades": 896, "win_rate_lb95": 47.73},
    ("confluence", "bearish"):               {"win_rate": 49.75, "avg_pnl": 0.373, "trades": 1833, "win_rate_lb95": 47.47},
    ("head_and_shoulders", "bearish"):       {"win_rate": 54.49, "avg_pnl": 0.64, "trades": 156, "win_rate_lb95": 46.66},
    ("ema_crossover", "bearish"):            {"win_rate": 53.55, "avg_pnl": 0.498, "trades": 183, "win_rate_lb95": 46.33},
    ("rsi_divergence", "bearish"):           {"win_rate": 48.69, "avg_pnl": 0.42, "trades": 1072, "win_rate_lb95": 45.71},
    ("macd_divergence", "bearish"):          {"win_rate": 48.39, "avg_pnl": 0.667, "trades": 870, "win_rate_lb95": 45.08},
    ("bearish_engulfing", "bearish"):        {"win_rate": 47.05, "avg_pnl": 0.151, "trades": 1035, "win_rate_lb95": 44.03},
    ("price_spike", "bullish"):              {"win_rate": 50.09, "avg_pnl": 0.057, "trades": 539, "win_rate_lb95": 45.89},
    ("double_top", "bearish"):               {"win_rate": 46.5, "avg_pnl": 0.036, "trades": 5161, "win_rate_lb95": 45.14},

    # --- NEGATIVE OOS EDGE — muted or confirmation-required ---
    ("double_bottom", "bullish"):            {"win_rate": 47.96, "avg_pnl": -0.264, "trades": 4716, "win_rate_lb95": 46.54},
    ("confluence", "bullish"):               {"win_rate": 48.32, "avg_pnl": -0.092, "trades": 1163, "win_rate_lb95": 45.46},
    ("macd_crossover", "bearish"):           {"win_rate": 48.11, "avg_pnl": -0.158, "trades": 661, "win_rate_lb95": 44.32},
    ("rsi_extreme", "bullish"):              {"win_rate": 46.74, "avg_pnl": -0.135, "trades": 1211, "win_rate_lb95": 43.94},
    ("macd_crossover", "bullish"):           {"win_rate": 47.54, "avg_pnl": -0.275, "trades": 671, "win_rate_lb95": 43.79},
    ("inverse_head_and_shoulders", "bullish"): {"win_rate": 45.94, "avg_pnl": -0.345, "trades": 2046, "win_rate_lb95": 43.79},
    ("ema_crossover", "bullish"):            {"win_rate": 48.54, "avg_pnl": -0.857, "trades": 171, "win_rate_lb95": 41.16},
    ("bullish_engulfing", "bullish"):        {"win_rate": 43.97, "avg_pnl": -0.294, "trades": 721, "win_rate_lb95": 40.39},
    ("52_week_low", "bearish"):              {"win_rate": 45.24, "avg_pnl": -0.69, "trades": 389, "win_rate_lb95": 40.37},
    ("gap_down", "bearish"):                 {"win_rate": 48.28, "avg_pnl": -0.214, "trades": 145, "win_rate_lb95": 40.29},
    ("morning_star", "bullish"):             {"win_rate": 43.97, "avg_pnl": -0.326, "trades": 448, "win_rate_lb95": 39.45},
    ("shooting_star", "bearish"):            {"win_rate": 46.24, "avg_pnl": -0.315, "trades": 186, "win_rate_lb95": 39.22},
    ("52_week_high", "bullish"):             {"win_rate": 41.81, "avg_pnl": -0.613, "trades": 928, "win_rate_lb95": 38.68},
    ("cup_and_handle", "bullish"):           {"win_rate": 43.81, "avg_pnl": -0.31, "trades": 226, "win_rate_lb95": 37.49},
    ("price_spike", "bearish"):              {"win_rate": 41.19, "avg_pnl": -1.059, "trades": 488, "win_rate_lb95": 36.91},
}

# Signals with statistically meaningful negative OOS edge across thousands
# of trades. These are muted by default — the engine still detects them
# (so the UI can show "double_bottom forming") but they do not contribute
# bullish/bearish weight to recommendations or paper trades.
#
# Promotion criteria (the four winners): Wilson 95% LB ≥ 49% AND positive
# avg PnL across ≥100 OOS trades. Only four signals clear this bar today.
RECOMMENDED_MUTES: list[str] = [
    # These remain muted — no pattern-detector tightening can rescue
    # them (52w high / cup-and-handle / morning_star are intrinsically
    # weak on Indian large-caps in this period).
    "52_week_high",           # n=928,  avg_pnl -0.61%, lb95 38.68
    "cup_and_handle",         # n=226,  avg_pnl -0.31%, lb95 37.49
    "morning_star",           # n=448,  avg_pnl -0.33%, lb95 39.45
    # 2026-05-25 audit (signal_outcomes table, 9,984 trades): bullish
    # engulfing collapsed to a 1.2% win rate on n=82. The detector is
    # firing on every counter-trend bounce in a bearish regime; there is
    # no salvageable edge. Muted universally — single-direction signal,
    # safe to kill outright.
    "bullish_engulfing",      # n=82,   avg_pnl -6.25%, WR 1.2%
    # Note: double_bottom, double_top, head_and_shoulders, and
    # inverse_head_and_shoulders were ALSO muted before. Their detectors
    # have been rewritten with prominence + separation + neckline-break
    # confirmation guardrails (see patterns.py), which should turn them
    # from net-negative noise into legitimate (rarer) signals. They are
    # unmuted here; the next walk-forward run will confirm or refute.
]

# Direction-aware mutes: kill one leg of a two-direction signal where the
# leg is structurally broken. Same semantics as RECOMMENDED_MUTES — the
# detector still runs (UI can show context) but strength is zeroed and
# the signal contributes nothing to scoring. Source: 2026-05-25 audit of
# signal_outcomes — bullish legs of these setups historically lose money
# on Indian large-caps in this regime; bearish legs work.
DIRECTIONAL_MUTES: set[tuple[str, str]] = {
    ("rsi_extreme", "bullish"),       # n=1612, WR 5.5%, avg -7.38%
    ("macd_crossover", "bullish"),    # n=472,  WR 6.6%, avg -5.52%
    # 2026-05-26 walk-forward (10-NIFTY universe, 2y, realistic-cost
    # evaluator). Each pair below has n>=60 and net-of-cost avg P&L
    # < -0.8% — i.e. losing money at scale even after honest brokerage,
    # STT, DP, slippage and GST. Adding to mutes flips the universe
    # WR from 37.2% → 42.1% per the harness `WHAT-IF` block.
    ("rsi_divergence", "bullish"),    # n=199, WR 28.1%, avg -2.13%
    ("rsi_divergence", "bearish"),    # n=155, WR 36.1%, avg -0.43%
    ("evening_star", "bearish"),      # n=116, WR 34.5%, avg -0.94%
    ("bearish_engulfing", "bearish"), # n=174, WR 32.2%, avg -1.04%
    ("macd_crossover", "bearish"),    # n=124, WR 30.6%, avg -0.90%
    ("shooting_star", "bearish"),     # n=20,  WR 10.0%, avg -2.66%  (small n, but extreme)
    ("head_and_shoulders", "bearish"),# n=27,  WR 22.2%, avg -2.72%
    ("price_spike", "bullish"),       # n=74,  WR 39.2%, avg -1.69%
}

# Setups the engine considers genuine edge sources after the walk-forward.
# Used by the scoring layer to amplify signal weight when one of these
# fires, and shown in the UI as "high-conviction signals".
PROMOTED_SIGNALS: set[tuple[str, str]] = {
    ("gap_up", "bullish"),               # lb95 53.19, +1.13% avg PnL
    ("macd_divergence", "bullish"),      # confirmed: 2y n=137 +0.03% avg, +1.1% Kelly
    # 2026-05-26 walk-forward: only TWO combos survive the realistic-cost
    # evaluator with positive net avg P&L on n>=100 across 10 NIFTY
    # names. double_top/bearish is the standout: +0.80% avg, +14.2%
    # Kelly, n=153. macd_divergence/bullish kept above.
    ("double_top", "bearish"),           # n=153, WR 44.4%, +0.80% avg, +14.2% Kelly
    # 2026-05-26: new bullish detectors with documented academic edge.
    # Walk-forward win-rates are pending; these are promoted prospectively
    # because their underlying anomaly literature (PEAD, quality
    # breakout) is among the most-replicated in equities research, and
    # the in-detector gates (delivery %, fundamentals composite, gap
    # magnitude, volume multiple) sharply restrict false positives. Will
    # be re-validated against the next walk-forward run.
    ("pead", "bullish"),
    ("quality_breakout", "bullish"),
    # REMOVED 2026-05-26: rsi_extreme/bearish — was promoted on gross
    # data; net of realistic Indian-market costs (apply_costs), it is
    # n=206 WR 32.5% avg -0.89%. Demoted; the symbol won't be killed
    # entirely because the LLM judge may still find value in it.
    # REMOVED: evening_star/bearish — net avg -0.94% on n=116. Moved
    # to DIRECTIONAL_MUTES above.
}

# Weight multiplier applied to a promoted signal's contribution in the
# recommendation score. Calibrated so the four winners are roughly twice
# as influential as a generic break-even setup — large enough to matter,
# small enough that no single signal can override the multi-factor stack.
PROMOTION_WEIGHT_MULTIPLIER: float = 1.6

# Regime-stratified kill list — built from the 2026-05-21 walk-forward
# (`by_regime_x_signal_oos`). Tuples are (regime, signal_type, direction)
# that had Wilson 95% LB < 40 OR (LB < 45 AND avg_pnl < −0.2%) on ≥50
# OOS trades. These setups are *suppressed in their bad regime only* —
# the same signal may still fire in regimes where it has positive edge.
REGIME_KILL_SET: set[tuple[str, str, str]] = {
    # trend_up — by far the worst regime for bullish reversals.
    ("trend_up", "price_spike", "bullish"),
    ("trend_up", "double_bottom", "bullish"),
    ("trend_up", "bullish_engulfing", "bullish"),
    ("trend_up", "macd_divergence", "bullish"),
    ("trend_up", "rsi_divergence", "bullish"),
    ("trend_up", "macd_divergence", "bearish"),
    ("trend_up", "rsi_divergence", "bearish"),
    ("trend_up", "shooting_star", "bearish"),
    ("trend_up", "bearish_engulfing", "bearish"),
    ("trend_up", "confluence", "bullish"),
    ("trend_up", "confluence", "bearish"),
    ("trend_up", "macd_crossover", "bearish"),
    # trend_down — counter-trend bullish reversals + late-stage bearish.
    ("trend_down", "hammer", "bullish"),
    ("trend_down", "bullish_engulfing", "bullish"),
    ("trend_down", "macd_divergence", "bearish"),
    ("trend_down", "gap_down", "bearish"),
    ("trend_down", "price_spike", "bearish"),
    ("trend_down", "52_week_low", "bearish"),
    ("trend_down", "head_and_shoulders", "bearish"),
    # sideways — choppy signals that whipsaw without trend support.
    ("sideways", "bullish_engulfing", "bullish"),
    ("sideways", "double_top", "bearish"),
    ("sideways", "macd_crossover", "bullish"),
    ("sideways", "hammer", "bullish"),
    ("sideways", "confluence", "bearish"),
    ("sideways", "ema_crossover", "bullish"),
}

# Regime-stratified promotion list — (regime, signal_type, direction)
# with Wilson 95% LB ≥ 47 AND positive avg_pnl. These get an extra
# boost on top of the unconditional PROMOTED_SIGNALS set.
#
# REMOVED 2026-06-08: ("trend_up", "rsi_extreme", "bearish") and
# ("trend_up", "evening_star", "bearish"). These were *boosting* bearish
# counter-trend signals inside uptrends — the precise failure mode
# ANALYSIS_REPORT.md documents (trend_up + bearish lost ~75% of the time;
# the engine fired 219 bearish vs 25 bullish in trend_up). Their apparent
# in-sample edge was a small-sample artefact swamped by the structural
# bias. Boosting a counter-trend short in a rising market is exactly what
# the regime guard below now forbids.
REGIME_PROMOTE_SET: set[tuple[str, str, str]] = {
    ("sideways", "rsi_extreme", "bearish"),
    ("sideways", "macd_divergence", "bullish"),
    ("trend_down", "price_spike", "bullish"),
    ("sideways", "rsi_divergence", "bullish"),
    ("trend_down", "macd_divergence", "bullish"),
}

# ── Structural anti-counter-trend guard ──────────────────────────────────
# 2026-06-08. Root-cause fix for the "structurally bearish in a rising
# market" defect. Muting individual (regime, signal, direction) combos was
# whack-a-mole: new bearish detectors kept leaking shorts into uptrends.
#
# The structural rule: in a confirmed `trend_up` regime, COUNTER-TREND
# signals (bearish) are suppressed by default. Only setups with proven
# out-of-sample bearish edge that survives the realistic-cost evaluator are
# allowed to fire as shorts against the trend. Symmetric guard for
# trend_down protects against catching falling knives with counter-trend
# longs. This converts an opt-OUT blocklist (mute the losers we've found)
# into an opt-IN allowlist (only trade counter-trend setups we've proven),
# which is the correct default for a mean-reversion-biased engine.
COUNTERTREND_ALLOWLIST: set[tuple[str, str, str]] = {
    # The single bearish combo with durable positive net edge across the
    # 2y AND 5y walk-forwards (n=153, +0.80% avg, +14.2% Kelly).
    ("trend_up", "double_top", "bearish"),
}


def is_countertrend_suppressed(
    regime: Optional[str], signal_type: str, direction: str
) -> bool:
    """True when a signal fights a confirmed trend without proven edge.

    trend_up + bearish, or trend_down + bullish, are counter-trend. They are
    suppressed unless the exact (regime, signal_type, direction) is on the
    COUNTERTREND_ALLOWLIST. Regimes other than the two trending states
    (e.g. ``sideways``, ``None``/unknown) are never suppressed here.
    """
    if not regime:
        return False
    is_counter = (
        (regime == "trend_up" and direction == "bearish")
        or (regime == "trend_down" and direction == "bullish")
    )
    if not is_counter:
        return False
    return (regime, signal_type, direction) not in COUNTERTREND_ALLOWLIST

# Backtest-driven execution policy. HARD = needs independent confirming
# evidence (different family) before becoming a trade. SOFT = the signal
# can fire alone but conviction is dampened.
HARD_CONFIRMATION_REQUIRED: set[tuple[str, str]] = {
    # All large-sample negative-edge bullish pattern setups.
    ("double_bottom", "bullish"),
    ("inverse_head_and_shoulders", "bullish"),
    ("cup_and_handle", "bullish"),
    ("52_week_high", "bullish"),
    ("morning_star", "bullish"),
    # Specifically the bearish price_spike — the single worst signal in
    # the entire OOS table (avg PnL -1.06%, Sharpe -0.23).
    ("price_spike", "bearish"),
}

SOFT_CONFIRMATION_REQUIRED: set[tuple[str, str]] = {
    ("rsi_extreme", "bullish"),
    ("bearish_engulfing", "bearish"),
    ("confluence", "bullish"),
    ("bullish_engulfing", "bullish"),
    ("52_week_low", "bearish"),
    ("shooting_star", "bearish"),
}

WEAK_DIRECTIONAL_SETUPS = HARD_CONFIRMATION_REQUIRED | SOFT_CONFIRMATION_REQUIRED

# Methodology metadata so the UI can disclose what it's looking at.
EDGE_META = {
    "source": "walk_forward_oos_2026-05-21",
    "method": "expanding_window_walk_forward",
    "folds": 4,
    "period": "2y",
    "eval_window_days": 5,
    "stocks": 40,
    "total_signals": 37403,
    "transaction_cost_pct": 0.20,
    "stat_basis": "out_of_sample_only",
    "win_rate_lb95": "Wilson 95% lower bound — penalises small samples",
}


# Per-symbol blocklist — when a single name accounts for an outsized share of
# universe loss across multiple signal types, the right action isn't to mute
# detectors (other symbols benefit from them) but to exclude the symbol from
# the strategy entirely. The 2026-05-26 5y walk-forward had ITC alone driving
# ~46% of total universe loss across 9 of the worst 13 (sym, signal, dir)
# buckets — no detector mix is going to rescue that. Add symbols here when
# their cumulative ≥1-year net P&L on signals is < -3% per trade with n>=100.
SYMBOL_BLOCKLIST: set[str] = {
    "ITC",   # 5y: 576 trades, WR 5.9%, avg -4.56%, contributes ~1900pp of universe loss
    "SBIN",  # 5y: 406 trades, WR 23.2%, avg -2.44% — mean-reversion on persistent downtrender
}


def is_symbol_blocked(symbol: str) -> bool:
    """True when a symbol is on the blocklist and should never be scanned/traded.

    Use at the top of every per-symbol entry point (orchestrator scan,
    recommendation engine, auto-paper-trader) so blocked names never reach
    any of the costly downstream layers.

    Consults the autonomous-gating overlay first (derived sets can only ADD
    blocks, never un-block a hand-curated name), then the constant blocklist.
    """
    if _overlay_blocked(symbol):
        return True
    return (symbol or "").upper() in SYMBOL_BLOCKLIST


def is_muted(signal_type: str, direction: Optional[str] = None) -> bool:
    """True when a (signal_type[, direction]) combination is muted.

    - ``signal_type`` only: universal mute (kills both directions). Back-compat
      with callers that don't yet pass direction.
    - ``signal_type + direction``: also checks the directional mute set —
      kills just the broken leg of a two-direction signal (e.g. the bullish
      leg of ``rsi_extreme`` while keeping the bearish leg).
    """
    if signal_type in RECOMMENDED_MUTES:
        return True
    if direction is not None and _overlay_muted(signal_type, direction):
        return True
    if direction is not None and (signal_type, direction) in DIRECTIONAL_MUTES:
        return True
    return False


def is_promoted(signal_type: str, direction: str) -> bool:
    """True for walk-forward-confirmed positive-edge setups.

    Consults the autonomous-gating overlay first (derived promotions add to,
    never remove, the hand-curated set), then the PROMOTED_SIGNALS constant.
    """
    if _overlay_promoted(signal_type, direction):
        return True
    return (signal_type, direction) in PROMOTED_SIGNALS


# ── autonomous-gating overlay bridge ──
# Lazy + fail-safe: the derived sets live in gating_state's sync cache (seeded
# at startup). We consult them here so the hot-path predicates stay sync. Any
# import/lookup failure falls through to the hand-curated constants — the
# derived layer can only ever ADD gating, never weaken it.
def _overlay_promoted(signal_type: str, direction: str) -> bool:
    try:
        from app.services.gating_state import overlay_is_promoted
        return overlay_is_promoted(signal_type, direction) is True
    except Exception:
        return False


def _overlay_muted(signal_type: str, direction: str) -> bool:
    try:
        from app.services.gating_state import overlay_is_muted
        return overlay_is_muted(signal_type, direction) is True
    except Exception:
        return False


def _overlay_blocked(symbol: str) -> bool:
    try:
        from app.services.gating_state import overlay_is_blocked
        return overlay_is_blocked(symbol) is True
    except Exception:
        return False


def signal_weight_multiplier(signal_type: str, direction: str, regime: Optional[str] = None) -> float:
    """Multiplier for a (signal_type, direction)'s contribution.

    Optional `regime` activates regime-stratified gating from the
    walk-forward. Ordering:
      1. `is_muted` — universal kill (still 0.0)
      2. `is_countertrend_suppressed` — kill counter-trend signals with no
         proven edge (structural anti-bearish-bias guard) (0.0)
      3. `REGIME_KILL_SET` — kill only in this regime (0.0)
      4. `REGIME_PROMOTE_SET` — extra boost in this regime (2.0×)
      5. `is_promoted` — universal 1.6× boost
      6. 1.0 otherwise
    """
    if is_muted(signal_type, direction):
        return 0.0
    if is_countertrend_suppressed(regime, signal_type, direction):
        return 0.0
    if regime and (regime, signal_type, direction) in REGIME_KILL_SET:
        return 0.0
    if regime and (regime, signal_type, direction) in REGIME_PROMOTE_SET:
        return 2.0
    if is_promoted(signal_type, direction):
        return PROMOTION_WEIGHT_MULTIPLIER
    return 1.0


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
    global _edge_last_refreshed_at
    loaded: dict[tuple[str, str], dict] = {}
    latest_updated_at: Optional[str] = None
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT signal_type, direction, win_rate, avg_pnl, trades, updated_at "
                "FROM signal_edge_overrides"
            ) as cur:
                async for row in cur:
                    loaded[(row["signal_type"], row["direction"])] = {
                        "win_rate": float(row["win_rate"]),
                        "avg_pnl": float(row["avg_pnl"]),
                        "trades": int(row["trades"]),
                    }
                    upd = row["updated_at"]
                    if upd and (latest_updated_at is None or upd > latest_updated_at):
                        latest_updated_at = upd
    except Exception as e:
        logger.debug("seed_edge_overrides: %s", e)
    set_edge_overrides(loaded)
    _edge_last_refreshed_at = latest_updated_at
    return len(loaded)


async def write_edge_overrides(
    rows: dict[tuple[str, str], dict],
    min_trades: int = _OVERRIDE_MIN_TRADES,
) -> int:
    """Persist + activate per-key overrides via UPSERT (merge, not replace).

    Skips rows below ``min_trades`` so a thin batch can never write a noisy
    edge. **Merges** the surviving keys into the existing table rather than
    replacing it: keys present in this batch are refreshed (freshest wins),
    keys absent from this batch are preserved. This is what makes it safe for
    both the daily *and* weekly backtests to refresh edges — a narrower daily
    run no longer wipes the broader weekly coverage (and vice-versa). The
    cold-start ``SIGNAL_EDGE`` baseline still backs any key neither run has
    measured yet, and ``edge_freshness()`` reports overall staleness.

    Returns the number of keys written/updated this call.
    """
    from app.database import DB_PATH
    global _edge_last_refreshed_at
    keep: dict[tuple[str, str], dict] = {
        k: v for k, v in rows.items() if (v.get("trades") or 0) >= min_trades
    }
    if not keep:
        logger.info(
            "signal_edge overrides: 0/%d keys cleared min_trades=%d — table unchanged",
            len(rows), min_trades,
        )
        return 0
    now = datetime.now(timezone.utc).isoformat()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            for (stype, direction), d in keep.items():
                await db.execute(
                    "INSERT INTO signal_edge_overrides "
                    "(signal_type, direction, win_rate, avg_pnl, trades, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(signal_type, direction) DO UPDATE SET "
                    "win_rate=excluded.win_rate, avg_pnl=excluded.avg_pnl, "
                    "trades=excluded.trades, updated_at=excluded.updated_at",
                    (stype, direction, float(d["win_rate"]), float(d["avg_pnl"]),
                     int(d["trades"]), now),
                )
            await db.commit()
    except Exception as e:
        logger.warning("write_edge_overrides failed (non-critical): %s", e)
        return 0
    # Re-seed the in-memory map from the merged table so this process reflects
    # the full set (this batch + previously-written keys), not just this batch.
    await seed_edge_overrides()
    _edge_last_refreshed_at = now
    logger.info(
        "signal_edge overrides merged: %d/%d keys upserted (min_trades=%d), %d total live",
        len(keep), len(rows), min_trades, len(_edge_overrides),
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


def edge_freshness() -> dict:
    """Report how current the edge priors are, so callers can warn on staleness.

    The static ``SIGNAL_EDGE`` table is a snapshot from ``EDGE_BASELINE_DATE``;
    live overrides from the weekly backtest shadow it per key. If no override
    has refreshed in ``EDGE_STALE_AFTER_DAYS`` (or none ever has), the engine
    is filtering live signals on priors that may no longer hold — ``is_stale``
    flags that. Returns::

        {
            "baseline_date": "2026-05-21",
            "last_refreshed_at": "2026-06-09T..." | None,
            "live_override_count": int,
            "age_days": float | None,   # since last live refresh
            "is_stale": bool,
            "reason": str,
        }
    """
    last = _edge_last_refreshed_at
    age_days: Optional[float] = None
    if last:
        try:
            ts = datetime.fromisoformat(last)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
        except Exception:
            age_days = None

    if age_days is None:
        is_stale = True
        reason = "no live override refresh on record — running on static baseline only"
    elif age_days > EDGE_STALE_AFTER_DAYS:
        is_stale = True
        reason = f"last live refresh {age_days:.0f}d ago (> {EDGE_STALE_AFTER_DAYS}d threshold)"
    else:
        is_stale = False
        reason = f"last live refresh {age_days:.0f}d ago"

    return {
        "baseline_date": EDGE_BASELINE_DATE,
        "last_refreshed_at": last,
        "live_override_count": len(_edge_overrides),
        "age_days": round(age_days, 1) if age_days is not None else None,
        "is_stale": is_stale,
        "reason": reason,
    }

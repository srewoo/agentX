from __future__ import annotations
"""
Backtesting framework for agentX signal engine.

Walks through historical data day-by-day, runs the existing signal detectors
on each slice, and measures how well signals predicted future price movement.

Uses yfinance (free) via the existing data_fetcher. No LLM calls.
"""
import logging
from typing import Any

import pandas as pd

from app.services.data_fetcher import async_fetch_history
from app.services.technicals import compute_technicals, compute_support_resistance
from app.services.signal_engine import scan_symbol
from app.utils import safe_float

logger = logging.getLogger(__name__)

# Minimum bars of history needed before we start generating signals.
# The technicals module needs at least 20 bars for RSI/SMA etc.
MIN_LOOKBACK = 26

# Signals with these directions count as directional bets.
BULLISH = "bullish"
BEARISH = "bearish"


def _evaluate_outcome(
    direction: str,
    entry_price: float,
    future_price: float,
) -> dict[str, float]:
    """Return pnl_pct and whether it was a win for the given direction."""
    if entry_price == 0:
        return {"pnl_pct": 0.0, "win": False}

    raw_pnl = (future_price - entry_price) / entry_price * 100

    if direction == BULLISH:
        pnl_pct = raw_pnl
    elif direction == BEARISH:
        pnl_pct = -raw_pnl
    else:
        # Neutral signals: track absolute move but count as non-directional
        pnl_pct = abs(raw_pnl)

    win = pnl_pct > 0
    return {"pnl_pct": round(pnl_pct, 4), "win": win}


async def run_backtest(
    symbol: str,
    period: str = "1y",
    eval_windows: list[int] | None = None,
) -> dict[str, Any]:
    """
    Run a historical backtest of the signal engine on a single symbol.

    Args:
        symbol: Stock symbol (e.g. "RELIANCE", "TCS").
        period: yfinance period string (default "1y").
        eval_windows: List of forward-looking day counts to evaluate
                      (default [1, 3, 5, 10]).

    Returns:
        Dict with per-signal-type and overall performance metrics.
    """
    if eval_windows is None:
        eval_windows = [1, 3, 5, 10]

    max_eval = max(eval_windows)

    logger.info("Backtest started: symbol=%s period=%s eval_windows=%s", symbol, period, eval_windows)

    # Fetch full history
    df = await async_fetch_history(symbol, period=period, interval="1d")
    if df is None or df.empty or len(df) < MIN_LOOKBACK + max_eval:
        logger.warning("Insufficient data for backtest: symbol=%s bars=%d", symbol, len(df) if df is not None else 0)
        return {
            "symbol": symbol,
            "period": period,
            "total_bars": len(df) if df is not None else 0,
            "total_signals": 0,
            "error": "Insufficient historical data for backtest.",
            "by_signal_type": {},
            "overall": {},
        }

    total_bars = len(df)
    close_values = df["Close"].values

    # Collect all signal outcomes
    # Structure: { signal_type: { direction: [ {window: {pnl_pct, win}, ...} ] } }
    results_by_type: dict[str, dict[str, list[dict]]] = {}
    total_signals = 0

    scan_end = total_bars - max_eval
    progress_step = max(1, (scan_end - MIN_LOOKBACK) // 10)

    for i in range(MIN_LOOKBACK, scan_end):
        if (i - MIN_LOOKBACK) % progress_step == 0:
            pct = round((i - MIN_LOOKBACK) / (scan_end - MIN_LOOKBACK) * 100)
            logger.info("Backtest progress: %s — %d%% (%d/%d bars)", symbol, pct, i - MIN_LOOKBACK, scan_end - MIN_LOOKBACK)

        # Slice: only data up to and including day i (simulates "today")
        window_df = df.iloc[: i + 1].copy()

        if len(window_df) < MIN_LOOKBACK:
            continue

        # Compute technicals and support/resistance on the known slice
        try:
            technicals = compute_technicals(window_df)
            sr = compute_support_resistance(window_df)
        except Exception as exc:
            logger.debug("Technicals failed at bar %d: %s", i, exc)
            continue

        # Previous close for price-spike detection
        prev_price = safe_float(close_values[i - 1]) if i > 0 else None

        # Run signal detectors (no sentiment — purely technical backtest)
        try:
            signals = scan_symbol(
                symbol=symbol,
                df=window_df,
                technicals=technicals,
                sr=sr,
                previous_price=prev_price,
                sentiment_score=None,
            )
        except Exception as exc:
            logger.debug("scan_symbol failed at bar %d: %s", i, exc)
            continue

        if not signals:
            continue

        entry_price = safe_float(close_values[i])
        if not entry_price or entry_price == 0:
            continue

        for sig in signals:
            signal_type = sig.get("signal_type", "unknown")
            direction = sig.get("direction", "neutral")
            total_signals += 1

            if signal_type not in results_by_type:
                results_by_type[signal_type] = {}
            if direction not in results_by_type[signal_type]:
                results_by_type[signal_type][direction] = []

            outcome: dict[str, Any] = {"bar_index": i, "entry_price": entry_price}
            for w in eval_windows:
                future_idx = i + w
                if future_idx < total_bars:
                    future_price = safe_float(close_values[future_idx])
                    if future_price:
                        result = _evaluate_outcome(direction, entry_price, future_price)
                        outcome[f"pnl_{w}d"] = result["pnl_pct"]
                        outcome[f"win_{w}d"] = result["win"]

            results_by_type[signal_type][direction].append(outcome)

    logger.info("Backtest complete: %s — %d signals found across %d bars", symbol, total_signals, total_bars)

    # Aggregate metrics
    by_signal_type = _aggregate_metrics(results_by_type, eval_windows)
    overall = _compute_overall(by_signal_type, eval_windows, total_signals)

    return {
        "symbol": symbol,
        "period": period,
        "total_bars": total_bars,
        "total_signals": total_signals,
        "by_signal_type": by_signal_type,
        "overall": overall,
    }


def _aggregate_metrics(
    results_by_type: dict[str, dict[str, list[dict]]],
    eval_windows: list[int],
) -> dict[str, Any]:
    """Aggregate per-signal-type, per-direction metrics."""
    aggregated: dict[str, Any] = {}

    for signal_type, directions in results_by_type.items():
        aggregated[signal_type] = {}
        for direction, outcomes in directions.items():
            total = len(outcomes)
            metrics: dict[str, Any] = {"total": total}

            for w in eval_windows:
                pnl_key = f"pnl_{w}d"
                win_key = f"win_{w}d"

                pnl_values = [o[pnl_key] for o in outcomes if pnl_key in o]
                win_values = [o[win_key] for o in outcomes if win_key in o]

                wins = sum(1 for v in win_values if v)
                losses = len(win_values) - wins
                evaluated = len(pnl_values)

                avg_pnl = round(sum(pnl_values) / len(pnl_values), 4) if pnl_values else 0.0
                win_rate = round(wins / evaluated * 100, 2) if evaluated > 0 else 0.0
                max_dd = round(min(pnl_values), 4) if pnl_values else 0.0

                metrics[f"wins_{w}d"] = wins
                metrics[f"losses_{w}d"] = losses
                metrics[f"win_rate_{w}d"] = win_rate
                metrics[f"avg_pnl_{w}d"] = avg_pnl
                metrics[f"max_drawdown_{w}d"] = max_dd

            aggregated[signal_type][direction] = metrics

    return aggregated


def _compute_overall(
    by_signal_type: dict[str, Any],
    eval_windows: list[int],
    total_signals: int,
) -> dict[str, Any]:
    """Compute overall summary metrics across all signal types."""
    if total_signals == 0:
        return {"total_signals": 0}

    overall: dict[str, Any] = {"total_signals": total_signals}

    # For each eval window, aggregate across all signal types
    for w in eval_windows:
        all_pnl: list[float] = []
        all_wins = 0
        all_evaluated = 0

        for signal_type, directions in by_signal_type.items():
            for direction, metrics in directions.items():
                wins_key = f"wins_{w}d"
                losses_key = f"losses_{w}d"
                avg_pnl_key = f"avg_pnl_{w}d"
                count = metrics.get("total", 0)

                if wins_key in metrics and losses_key in metrics:
                    all_wins += metrics[wins_key]
                    all_evaluated += metrics[wins_key] + metrics[losses_key]

                if avg_pnl_key in metrics and count > 0:
                    # Weight by count for proper averaging
                    all_pnl.extend([metrics[avg_pnl_key]] * count)

        overall_win_rate = round(all_wins / all_evaluated * 100, 2) if all_evaluated > 0 else 0.0
        overall_avg_pnl = round(sum(all_pnl) / len(all_pnl), 4) if all_pnl else 0.0

        # Find max drawdown across all signal types for this window
        all_drawdowns: list[float] = []
        for directions in by_signal_type.values():
            for metrics in directions.values():
                dd_key = f"max_drawdown_{w}d"
                if dd_key in metrics:
                    all_drawdowns.append(metrics[dd_key])
        max_dd = round(min(all_drawdowns), 4) if all_drawdowns else 0.0

        overall[f"win_rate_{w}d"] = overall_win_rate
        overall[f"avg_pnl_{w}d"] = overall_avg_pnl
        overall[f"max_drawdown_{w}d"] = max_dd

    # Best and worst signal type by 5d avg_pnl (or first available window)
    reference_window = 5 if 5 in eval_windows else eval_windows[0]
    ref_key = f"avg_pnl_{reference_window}d"

    type_scores: list[tuple[str, float]] = []
    for signal_type, directions in by_signal_type.items():
        pnl_sum = 0.0
        count = 0
        for direction, metrics in directions.items():
            if ref_key in metrics:
                pnl_sum += metrics[ref_key] * metrics.get("total", 1)
                count += metrics.get("total", 1)
        if count > 0:
            type_scores.append((signal_type, round(pnl_sum / count, 4)))

    if type_scores:
        type_scores.sort(key=lambda x: x[1], reverse=True)
        overall["best_signal_type"] = type_scores[0][0]
        overall["worst_signal_type"] = type_scores[-1][0]
    else:
        overall["best_signal_type"] = None
        overall["worst_signal_type"] = None

    return overall

from __future__ import annotations
"""
Signal outcome tracker — evaluates whether BUY/SELL signals made money.
Fetches current prices for past signals, calculates PnL, and stores outcomes.
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

import aiosqlite

from app.database import DB_PATH
from app.services.data_fetcher import async_fetch_history
from app.utils import safe_float

logger = logging.getLogger(__name__)

# Max concurrent yfinance calls during evaluation
_EVAL_SEMAPHORE = asyncio.Semaphore(3)

# Evaluation windows in days — signal is checked at each milestone
EVAL_WINDOWS = [1, 3, 7, 30]


async def evaluate_signals() -> dict[str, Any]:
    """
    Evaluate open signals from the last 1-30 days.

    For each unevaluated signal:
    1. Fetch current price via yfinance
    2. Calculate PnL based on direction (bullish/bearish)
    3. Determine outcome (win/loss/open/expired)
    4. Store in signal_outcomes
    5. Recalculate aggregate performance stats

    Returns summary of evaluation run.
    """
    now = datetime.now(timezone.utc)
    cutoff_start = (now - timedelta(days=30)).isoformat()
    cutoff_recent = (now - timedelta(days=1)).isoformat()

    # Load signals from 1-30 days ago that are not yet fully resolved
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT s.id, s.symbol, s.signal_type, s.direction,
                   s.current_price, s.created_at
            FROM signals s
            WHERE s.created_at >= ?
              AND s.created_at <= ?
              AND s.id NOT IN (
                  SELECT signal_id FROM signal_outcomes
                  WHERE outcome IN ('win', 'loss', 'expired')
              )
            ORDER BY s.created_at ASC
        """
        async with db.execute(query, (cutoff_start, cutoff_recent)) as cursor:
            rows = await cursor.fetchall()
            signals_to_eval = [dict(row) for row in rows]

    if not signals_to_eval:
        logger.info("No signals to evaluate")
        return {"evaluated": 0, "skipped": 0, "errors": 0}

    logger.info(f"Evaluating {len(signals_to_eval)} signals for outcomes")

    # Group by symbol to minimize yfinance calls
    symbol_signals: dict[str, list[dict]] = {}
    for sig in signals_to_eval:
        symbol_signals.setdefault(sig["symbol"], []).append(sig)

    evaluated = 0
    skipped = 0
    errors = 0
    outcomes: list[dict] = []

    async def fetch_and_evaluate(symbol: str, sigs: list[dict]) -> None:
        nonlocal evaluated, skipped, errors

        async with _EVAL_SEMAPHORE:
            try:
                df = await async_fetch_history(symbol, "5d", "1d")
                if df is None or df.empty:
                    logger.warning(f"No price data for {symbol}, skipping evaluation")
                    skipped += len(sigs)
                    return

                current_price = safe_float(df["Close"].iloc[-1])
                if current_price is None:
                    skipped += len(sigs)
                    return

                for sig in sigs:
                    try:
                        outcome = _compute_outcome(sig, current_price, now)
                        if outcome:
                            outcomes.append(outcome)
                            evaluated += 1
                        else:
                            skipped += 1
                    except Exception as e:
                        logger.warning(f"Error evaluating signal {sig['id']}: {e}")
                        errors += 1
            except Exception as e:
                logger.warning(f"Error fetching price for {symbol}: {e}")
                errors += len(sigs)
            finally:
                # Rate limit yfinance calls
                await asyncio.sleep(0.5)

    # Process symbols sequentially in small batches to avoid hammering yfinance
    symbols = list(symbol_signals.keys())
    batch_size = 5
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i : i + batch_size]
        tasks = [
            fetch_and_evaluate(sym, symbol_signals[sym]) for sym in batch
        ]
        await asyncio.gather(*tasks)

    # Store outcomes
    if outcomes:
        await _store_outcomes(outcomes)

    # Recalculate aggregate stats
    await _recalculate_performance()

    summary = {"evaluated": evaluated, "skipped": skipped, "errors": errors}
    logger.info(f"Signal evaluation complete: {summary}")
    return summary


def _compute_outcome(
    sig: dict, current_price: float, now: datetime
) -> dict | None:
    """Compute PnL and outcome for a single signal."""
    entry_price = safe_float(sig["current_price"])
    if entry_price is None or entry_price == 0:
        return None

    direction = sig["direction"]

    # Neutral signals (e.g. volume_spike) cannot be evaluated for PnL
    if direction == "neutral":
        return {
            "signal_id": sig["id"],
            "symbol": sig["symbol"],
            "signal_type": sig["signal_type"],
            "direction": direction,
            "entry_price": entry_price,
            "exit_price": current_price,
            "entry_time": sig["created_at"],
            "exit_time": now.isoformat(),
            "pnl_pct": None,
            "outcome": "expired",
            "hold_days": _days_since(sig["created_at"], now),
            "evaluated_at": now.isoformat(),
        }

    # Calculate PnL based on direction
    if direction == "bullish":
        pnl_pct = ((current_price - entry_price) / entry_price) * 100
    elif direction == "bearish":
        pnl_pct = ((entry_price - current_price) / entry_price) * 100
    else:
        return None

    pnl_pct = round(pnl_pct, 2)
    hold_days = _days_since(sig["created_at"], now)

    # Determine outcome
    if hold_days >= 30:
        outcome = "expired"
    elif pnl_pct > 0:
        outcome = "win"
    elif pnl_pct <= 0:
        outcome = "loss"
    else:
        outcome = "open"

    return {
        "signal_id": sig["id"],
        "symbol": sig["symbol"],
        "signal_type": sig["signal_type"],
        "direction": direction,
        "entry_price": entry_price,
        "exit_price": current_price,
        "entry_time": sig["created_at"],
        "exit_time": now.isoformat(),
        "pnl_pct": pnl_pct,
        "outcome": outcome,
        "hold_days": hold_days,
        "evaluated_at": now.isoformat(),
    }


def _days_since(created_at: str, now: datetime) -> int:
    """Calculate days between created_at ISO string and now."""
    try:
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        delta = now - created
        return max(0, delta.days)
    except Exception:
        return 0


async def _store_outcomes(outcomes: list[dict]) -> None:
    """Persist signal outcomes to database (upsert)."""
    async with aiosqlite.connect(DB_PATH) as db:
        for o in outcomes:
            await db.execute(
                """INSERT OR REPLACE INTO signal_outcomes
                   (signal_id, symbol, signal_type, direction, entry_price,
                    exit_price, entry_time, exit_time, pnl_pct, outcome,
                    hold_days, evaluated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    o["signal_id"], o["symbol"], o["signal_type"], o["direction"],
                    o["entry_price"], o["exit_price"], o["entry_time"],
                    o["exit_time"], o["pnl_pct"], o["outcome"],
                    o["hold_days"], o["evaluated_at"],
                ),
            )
        await db.commit()
    logger.info(f"Stored {len(outcomes)} signal outcomes")


async def _recalculate_performance() -> None:
    """Recalculate aggregate performance stats from signal_outcomes."""
    now_iso = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Aggregate by (signal_type, direction) for resolved outcomes
        query = """
            SELECT signal_type, direction,
                   COUNT(*) as total,
                   SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) as losses,
                   AVG(CASE WHEN pnl_pct IS NOT NULL THEN pnl_pct ELSE 0 END) as avg_pnl
            FROM signal_outcomes
            WHERE outcome IN ('win', 'loss', 'expired')
            GROUP BY signal_type, direction
        """
        async with db.execute(query) as cursor:
            rows = await cursor.fetchall()

        for row in rows:
            total = row["total"]
            wins = row["wins"]
            win_rate = round((wins / total) * 100, 1) if total > 0 else 0.0
            avg_pnl = round(row["avg_pnl"], 2) if row["avg_pnl"] else 0.0

            await db.execute(
                """INSERT OR REPLACE INTO signal_performance
                   (signal_type, direction, timeframe, total_signals, wins,
                    losses, avg_pnl_pct, win_rate, updated_at)
                   VALUES (?, ?, 'all', ?, ?, ?, ?, ?, ?)""",
                (
                    row["signal_type"], row["direction"],
                    total, wins, row["losses"], avg_pnl, win_rate, now_iso,
                ),
            )

        await db.commit()
    logger.info("Performance stats recalculated")


async def get_performance_stats() -> list[dict[str, Any]]:
    """Return performance breakdown by signal_type."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT signal_type, direction, timeframe, total_signals,
                   wins, losses, avg_pnl_pct, win_rate, updated_at
            FROM signal_performance
            ORDER BY total_signals DESC
        """
        async with db.execute(query) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_signal_accuracy(
    signal_type: str, direction: str
) -> dict[str, Any]:
    """Return accuracy stats for a specific signal_type + direction."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT signal_type, direction, timeframe, total_signals,
                   wins, losses, avg_pnl_pct, win_rate, updated_at
            FROM signal_performance
            WHERE signal_type = ? AND direction = ?
        """
        async with db.execute(query, (signal_type, direction)) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return {
                "signal_type": signal_type,
                "direction": direction,
                "timeframe": "all",
                "total_signals": 0,
                "wins": 0,
                "losses": 0,
                "avg_pnl_pct": 0.0,
                "win_rate": 0.0,
                "updated_at": None,
            }


async def get_performance_summary() -> dict[str, Any]:
    """Return overall summary: total evaluated, overall win rate, avg PnL."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT
                COUNT(*) as total_evaluated,
                SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as total_wins,
                SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) as total_losses,
                AVG(CASE WHEN pnl_pct IS NOT NULL THEN pnl_pct ELSE 0 END) as avg_pnl,
                SUM(CASE WHEN outcome = 'open' THEN 1 ELSE 0 END) as open_signals
            FROM signal_outcomes
        """
        async with db.execute(query) as cursor:
            row = await cursor.fetchone()

        if not row or row["total_evaluated"] == 0:
            return {
                "total_evaluated": 0,
                "total_wins": 0,
                "total_losses": 0,
                "open_signals": 0,
                "win_rate": 0.0,
                "avg_pnl_pct": 0.0,
            }

        total = row["total_evaluated"]
        wins = row["total_wins"] or 0
        resolved = wins + (row["total_losses"] or 0)
        win_rate = round((wins / resolved) * 100, 1) if resolved > 0 else 0.0

        return {
            "total_evaluated": total,
            "total_wins": wins,
            "total_losses": row["total_losses"] or 0,
            "open_signals": row["open_signals"] or 0,
            "win_rate": win_rate,
            "avg_pnl_pct": round(row["avg_pnl"] or 0, 2),
        }

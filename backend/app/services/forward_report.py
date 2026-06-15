from __future__ import annotations
"""D2–D4 — benchmark-relative forward reporting, readiness, durability.

The D1 decision log and the closed paper trades are only useful if we read them
honestly. This module turns them into the numbers that decide whether the
system is actually working forward:

  * **D2 benchmark-relative performance** — forward expectancy, win rate (with
    a Wilson CI), per-trade Sharpe, max drawdown, and alpha vs a benchmark
    return. The point is to compare *forward* results against the *backtest*
    confidence interval and catch the train→test collapse early.
  * **D3 readiness gate** — n closed / target. Below the target we refuse to
    report a win rate as if it meant something; a 9-trade "60% win rate" is
    noise and saying so out loud prevents false confidence.
  * **D4 durability check** — is the forward win rate inside the backtest's
    confidence interval? If the backtest claimed 50% and forward is 38% with
    no CI overlap, that's a divergence alert, not a rounding error.

Pure stats are separated from the DB aggregator so they unit-test cleanly.
"""
import logging
import math
from typing import Optional

import aiosqlite

from app.database import DB_PATH

logger = logging.getLogger(__name__)

DEFAULT_TARGET_TRADES = 300  # per the plan: ~200-400 closed before stats mean anything


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson (lo, hi) confidence interval for a win rate. (0,0) for empty."""
    if n <= 0:
        return (0.0, 0.0)
    phat = max(0.0, min(1.0, wins / n))
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = phat + z2 / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z2 / (4 * n)) / n)
    return (max(0.0, (centre - margin) / denom), min(1.0, (centre + margin) / denom))


def expectancy(pnls: list[float]) -> float:
    """Mean per-trade PnL (the expectancy). 0.0 for empty."""
    return sum(pnls) / len(pnls) if pnls else 0.0


def sharpe(returns: list[float]) -> float:
    """Per-trade Sharpe = mean / stdev of trade returns. 0.0 if undefined."""
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    sd = math.sqrt(var)
    return round(mean / sd, 4) if sd > 0 else 0.0


def max_drawdown(equity_curve: list[float]) -> float:
    """Max peak-to-trough drawdown of an equity curve, as a positive %."""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    worst = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak * 100.0)
    return round(worst, 3)


def readiness(n: int, target: int = DEFAULT_TARGET_TRADES) -> dict:
    """D3 readiness gate: is the sample big enough to trust a win rate?"""
    return {
        "n": n,
        "target": target,
        "ready": n >= target,
        "progress_pct": round(min(100.0, n / target * 100.0), 1) if target else 0.0,
        "message": (
            f"{n}/{target} closed trades — statistics are reliable"
            if n >= target else
            f"{n}/{target} closed trades — insufficient sample, win rate not yet meaningful"
        ),
    }


def durability_verdict(
    forward_wins: int, forward_n: int, backtest_win_rate: float
) -> dict:
    """D4: does the backtest win rate fall inside the forward Wilson CI?

    No overlap ⇒ the forward result diverges from what the backtest promised
    (the train→test collapse signature). Returns a verdict + the interval.
    """
    lo, hi = wilson_interval(forward_wins, forward_n)
    inside = lo <= backtest_win_rate <= hi
    return {
        "forward_win_rate": round(forward_wins / forward_n, 4) if forward_n else 0.0,
        "forward_ci": [round(lo, 4), round(hi, 4)],
        "backtest_win_rate": round(backtest_win_rate, 4),
        "diverged": (not inside) and forward_n > 0,
        "verdict": (
            "insufficient_data" if forward_n == 0 else
            "consistent" if inside else "DIVERGED — forward below backtest CI"
        ),
    }


async def _closed_trades(db_path: str) -> list[dict]:
    """Closed paper trades with pnl, ordered by exit time."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT pnl_pct, pnl_amount, exit_date FROM paper_trades "
            "WHERE status='closed' AND pnl_pct IS NOT NULL "
            "ORDER BY exit_date ASC, trade_id ASC"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def forward_performance(
    *,
    db_path: Optional[str] = None,
    capital: float = 100_000.0,
    benchmark_return_pct: Optional[float] = None,
    target_trades: int = DEFAULT_TARGET_TRADES,
) -> dict:
    """D2: benchmark-relative forward performance with readiness (D3).

    Reads closed paper trades. Win rate carries a Wilson CI; Sharpe and max
    drawdown describe the equity curve; alpha is expectancy minus the supplied
    benchmark return. ``ready`` (D3) flags whether the sample is large enough
    to trust — callers should suppress the win rate in the UI when not ready.
    """
    path = db_path or DB_PATH
    trades = await _closed_trades(path)
    n = len(trades)
    pnl_pcts = [float(t["pnl_pct"]) for t in trades]
    wins = sum(1 for p in pnl_pcts if p > 0)
    lo, hi = wilson_interval(wins, n)

    equity = [capital]
    for t in trades:
        equity.append(equity[-1] + float(t.get("pnl_amount") or 0.0))

    exp = round(expectancy(pnl_pcts), 4)
    report = {
        "trades": n,
        "wins": wins,
        "win_rate": round(wins / n, 4) if n else 0.0,
        "win_rate_ci": [round(lo, 4), round(hi, 4)],
        "expectancy_pct": exp,
        "sharpe_per_trade": sharpe(pnl_pcts),
        "max_drawdown_pct": max_drawdown(equity),
        "total_pnl": round(equity[-1] - capital, 2),
        "readiness": readiness(n, target_trades),
    }
    if benchmark_return_pct is not None:
        report["benchmark_return_pct"] = round(benchmark_return_pct, 4)
        report["alpha_pct"] = round(exp - benchmark_return_pct, 4)
    return report


async def durability_check(
    *, db_path: Optional[str] = None, backtest_win_rate: float = 0.50
) -> dict:
    """D4: compare the forward win rate against a backtest baseline."""
    path = db_path or DB_PATH
    trades = await _closed_trades(path)
    wins = sum(1 for t in trades if float(t["pnl_pct"]) > 0)
    return durability_verdict(wins, len(trades), backtest_win_rate)

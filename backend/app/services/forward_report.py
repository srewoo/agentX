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


def expectancy_lower_bound(pnls: list[float], z: float = 1.96) -> float:
    """95% lower confidence bound on mean per-trade PnL (the expectancy).

    The win-rate carries a Wilson interval (binomial); expectancy is a *mean*,
    so its uncertainty is the standard error of the mean: ``mean − z·s/√n``
    (normal approximation, fine for n≥30). This is the number that answers the
    only question that matters — "is the edge above zero *with confidence*?" —
    so a lucky positive mean on a thin, noisy sample can't masquerade as edge.
    Returns 0.0 for n<2 (no dispersion estimate).
    """
    n = len(pnls)
    if n < 2:
        return 0.0
    mean = sum(pnls) / n
    var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
    se = math.sqrt(var) / math.sqrt(n)
    return mean - z * se


def sharpe(returns: list[float]) -> float:
    """Per-trade Sharpe = mean / stdev of trade returns. 0.0 if undefined."""
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    sd = math.sqrt(var)
    return round(mean / sd, 4) if sd > 0 else 0.0


def beta(trade_rets: list[float], bench_rets: list[float]) -> Optional[float]:
    """Book beta vs the benchmark: cov(trade, bench) / var(bench).

    A win rate reported without beta is marketing — a beta-1 long book in a +10%
    tape should make money doing nothing. Returns None when undefined (n<2 or the
    benchmark had no variance)."""
    n = min(len(trade_rets), len(bench_rets))
    if n < 2:
        return None
    tr, br = trade_rets[:n], bench_rets[:n]
    mt, mb = sum(tr) / n, sum(br) / n
    var_b = sum((b - mb) ** 2 for b in br) / (n - 1)
    if var_b <= 0:
        return None
    cov = sum((tr[i] - mt) * (br[i] - mb) for i in range(n)) / (n - 1)
    return round(cov / var_b, 4)


def attach_benchmark_excess(trades: list[dict], bench_closes) -> int:
    """Stamp per-trade ``bench_ret`` and ``excess_pnl`` in place, vs NIFTY.

    Mirrors the walk-forward attribution (``backtester_walk_forward._attach_benchmark``)
    so forward and backtest speak the same language: for each closed trade we take
    the index return over the trade's actual holding window and subtract the
    direction-aware index move from the trade's pnl. A short's alpha is its pnl
    PLUS the index move it was fighting (``sign = -1`` for bearish).

    ``bench_closes`` is a pandas Series of index closes indexed by date. Fail-open:
    trades whose window can't be resolved are left un-stamped. Returns the count
    of trades that received attribution.
    """
    if bench_closes is None or len(bench_closes) == 0 or not trades:
        return 0
    import pandas as pd

    idx = pd.DatetimeIndex(pd.to_datetime(bench_closes.index)).tz_localize(None)
    vals = list(bench_closes.values)
    stamped = 0
    for t in trades:
        entry_raw, exit_raw = t.get("entry_date"), t.get("exit_date")
        if not entry_raw or not exit_raw:
            continue
        try:
            entry_dt = pd.to_datetime(entry_raw).tz_localize(None)
            exit_dt = pd.to_datetime(exit_raw).tz_localize(None)
        except (ValueError, TypeError):
            continue
        # Last index bar at or before each timestamp (searchsorted 'right' − 1).
        entry_pos = int(idx.searchsorted(entry_dt, side="right")) - 1
        exit_pos = int(idx.searchsorted(exit_dt, side="right")) - 1
        if entry_pos < 0 or exit_pos <= entry_pos or exit_pos >= len(vals):
            continue
        entry_b = float(vals[entry_pos])
        if entry_b <= 0:
            continue
        bench_ret = (float(vals[exit_pos]) - entry_b) / entry_b * 100.0
        sign = -1.0 if t.get("direction") == "bearish" else 1.0
        t["bench_ret"] = round(bench_ret, 4)
        t["excess_pnl"] = round(float(t.get("pnl_pct") or 0.0) - sign * bench_ret, 4)
        stamped += 1
    return stamped


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


def window_integrity(daily_log_rows: list[dict]) -> dict:
    """Summarise forward-window integrity from daily_log.csv rows.

    The paper-trade routine records every missed weekday run as a
    ``status=MISSED_RUN`` row (see ``paper_trade.sh record_run_gaps``). A verdict
    computed over a window pocked with missed runs is degraded, so we surface the
    gap count and a clean-fraction the caller can use to discount it. Rows with an
    absent/blank status are pre-migration completed days and count as ``ok``.
    """
    total = len(daily_log_rows)
    missed = [r.get("date") for r in daily_log_rows
              if (r.get("status") or "").strip().upper() == "MISSED_RUN"]
    logged = total - len(missed)
    return {
        "logged_days": logged,
        "missed_runs": len(missed),
        "missed_dates": [d for d in missed if d],
        "clean_fraction": round(logged / total, 4) if total else 1.0,
        "degraded": len(missed) > 0,
    }


async def live_combo_records(db_path: Optional[str] = None) -> dict[str, tuple[int, int]]:
    """Forward (paper-trade) win record per ``signal_type|direction`` combo.

    Returns ``{key: (wins, n)}`` over CLOSED paper trades — the live evidence the
    gating kill rule and re-promotion bar consume. Neutral direction is skipped
    (it has no directional edge to kill).
    """
    path = db_path or DB_PATH
    out: dict[str, tuple[int, int]] = {}
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT signal_type, direction, "
            "COUNT(*) AS n, "
            "SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) AS wins "
            "FROM paper_trades "
            "WHERE status='closed' AND pnl_pct IS NOT NULL AND direction != 'neutral' "
            "GROUP BY signal_type, direction"
        ) as cur:
            for r in await cur.fetchall():
                key = f"{r['signal_type']}|{r['direction']}"
                out[key] = (int(r["wins"] or 0), int(r["n"] or 0))
    return out


def classify_regime(trailing_return_pct: float, threshold_pct: float = 3.0) -> str:
    """Regime label from a trailing index return: trend_up / trend_down / sideways.

    Pure and reproducible — derived from the index, not a hand-curated set, so an
    edge that only exists in one regime is gated BY DATA (4.4)."""
    if trailing_return_pct >= threshold_pct:
        return "trend_up"
    if trailing_return_pct <= -threshold_pct:
        return "trend_down"
    return "sideways"


def regime_stratified(trades: list[dict]) -> dict[str, Any]:
    """Per-regime forward verdict with Wilson bounds. Each trade needs a
    ``regime`` and ``pnl_pct``. An edge confined to one regime shows up here."""
    out: dict[str, Any] = {}
    by_regime: dict[str, list[float]] = {}
    for t in trades:
        reg = t.get("regime") or "unknown"
        by_regime.setdefault(reg, []).append(float(t.get("pnl_pct") or 0.0))
    for reg, pnls in by_regime.items():
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        lo, hi = wilson_interval(wins, n)
        out[reg] = {
            "n": n, "wins": wins,
            "win_rate": round(wins / n, 4) if n else 0.0,
            "win_rate_lb": round(lo, 4),
            "win_rate_ci": [round(lo, 4), round(hi, 4)],
            "expectancy_pct": round(expectancy(pnls), 4),
        }
    return out


async def regime_stratified_verdict(
    *, db_path: Optional[str] = None, lookback: int = 20, threshold_pct: float = 3.0,
) -> dict[str, Any]:
    """4.4 — the forward verdict split by market regime at each trade's entry.

    Regime is classified from the NIFTY trailing return over ``lookback`` days
    at the trade's entry date, so the split is data-driven and reproducible.
    """
    path = db_path or DB_PATH
    trades = await _closed_trades(path)
    if not trades:
        return {"regimes": {}, "note": "no closed trades"}
    bench = await _benchmark_closes_for(trades)
    if bench is not None and len(bench):
        import pandas as pd
        idx = pd.DatetimeIndex(pd.to_datetime(bench.index)).tz_localize(None)
        vals = list(bench.values)
        for t in trades:
            try:
                entry_dt = pd.to_datetime(t.get("entry_date")).tz_localize(None)
                pos = int(idx.searchsorted(entry_dt, side="right")) - 1
                if pos - lookback >= 0 and pos < len(vals):
                    prev = float(vals[pos - lookback])
                    ret = (float(vals[pos]) - prev) / prev * 100.0 if prev > 0 else 0.0
                    t["regime"] = classify_regime(ret, threshold_pct)
                else:
                    t["regime"] = "unknown"
            except (ValueError, TypeError):
                t["regime"] = "unknown"
    return {"regimes": regime_stratified(trades),
            "lookback_days": lookback, "threshold_pct": threshold_pct}


async def _closed_trades(db_path: str) -> list[dict]:
    """Closed paper trades with pnl, ordered by exit time.

    ``direction`` and ``entry_date`` come along so benchmark attribution can
    reconstruct each trade's index-relative window.
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT pnl_pct, pnl_amount, direction, entry_date, exit_date "
            "FROM paper_trades "
            "WHERE status='closed' AND pnl_pct IS NOT NULL "
            "ORDER BY exit_date ASC, trade_id ASC"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


_BENCH_SYMBOL = "^NSEI"


async def _benchmark_closes_for(trades: list[dict]):
    """NIFTY daily closes covering the span of ``trades``. None on any failure."""
    if not trades:
        return None
    import pandas as pd

    dates = []
    for t in trades:
        for key in ("entry_date", "exit_date"):
            try:
                dates.append(pd.to_datetime(t.get(key)).tz_localize(None))
            except (ValueError, TypeError):
                pass
    if not dates:
        return None
    # Pick the smallest history bucket that spans earliest entry → now, padded.
    from datetime import datetime, timezone

    span_days = (datetime.now(timezone.utc).replace(tzinfo=None) - min(dates)).days + 5
    period = next(
        (p for p, d in (("1mo", 35), ("3mo", 100), ("6mo", 200), ("1y", 370),
                        ("2y", 740), ("5y", 1850)) if d >= span_days),
        "5y",
    )
    try:
        from app.services.data_fetcher import async_fetch_history

        bdf = await async_fetch_history(_BENCH_SYMBOL, period=period, interval="1d")
        return bdf["Close"] if bdf is not None and not bdf.empty else None
    except Exception:
        logger.warning("forward benchmark fetch failed", exc_info=True)
        return None


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
        "expectancy_lb95_pct": round(expectancy_lower_bound(pnl_pcts), 4),
        "sharpe_per_trade": sharpe(pnl_pcts),
        "max_drawdown_pct": max_drawdown(equity),
        "total_pnl": round(equity[-1] - capital, 2),
        "readiness": readiness(n, target_trades),
    }

    # Per-trade benchmark attribution vs NIFTY, matching walk-forward. A
    # long-biased book in a rising tape looks like alpha until every trade is
    # measured against holding the index over the same window.
    bench_closes = await _benchmark_closes_for(trades)
    stamped = attach_benchmark_excess(trades, bench_closes)
    if stamped:
        excess = [float(t["excess_pnl"]) for t in trades if "excess_pnl" in t]
        excess_wins = sum(1 for e in excess if e > 0)
        elo, ehi = wilson_interval(excess_wins, len(excess))
        # Book beta vs NIFTY over the attributed trades (direction-aware pnl vs
        # index move) so the win rate is read next to the market exposure it carried.
        attributed = [t for t in trades if "excess_pnl" in t and "bench_ret" in t]
        book_beta = beta([float(t["pnl_pct"]) for t in attributed],
                         [float(t["bench_ret"]) for t in attributed])
        report["benchmark"] = {
            "symbol": _BENCH_SYMBOL,
            "attributed_trades": stamped,
            "excess_expectancy_pct": round(expectancy(excess), 4),
            "excess_expectancy_lb95_pct": round(expectancy_lower_bound(excess), 4),
            "excess_win_rate": round(excess_wins / len(excess), 4) if excess else 0.0,
            "excess_win_rate_ci": [round(elo, 4), round(ehi, 4)],
            "excess_sharpe_per_trade": sharpe(excess),
            "beta_vs_benchmark": book_beta,
            "note": ("excess_pnl is direction-aware (short alpha = pnl + index move); "
                     "read excess win rate alongside beta_vs_benchmark, not the "
                     "absolute win rate"),
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

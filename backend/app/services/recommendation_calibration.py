from __future__ import annotations

"""Large-scale walk-forward calibration for the recommendation engine.

This is deliberately offline/batch-oriented. It uses free EOD history through
`data_fetcher`, slices each symbol walk-forward, generates historical
recommendation-like calls, evaluates forward SL/target/time-expiry outcomes,
then writes factor edge into `factor_performance`.
"""

import asyncio
import json
import logging
import math
from datetime import datetime, timezone
from typing import Any, Literal

import aiosqlite
import pandas as pd

from app.database import DB_PATH
from app.models.recommendation import Horizon, SignalContribution
from app.services.multiple_testing import benjamini_hochberg
from app.services.backtester import TRANSACTION_COST_PCT
from app.services.data_fetcher import MAJOR_STOCKS, async_fetch_history
from app.services.recommendation import (
    WEIGHTS_CALM,
    _market_regime,
    _score_all,
    _select_weights,
    action_from_score,
    calibrated_conviction,
)
from app.services.recommendation_factors import entry_sl_targets
from app.services.recommendation_tracker import seed_factor_edge_cache
from app.services.technicals import compute_technicals

logger = logging.getLogger(__name__)

UniverseName = Literal["nifty50", "nifty100", "nifty500", "curated"]

_HORIZON_DAYS: dict[Horizon, int] = {"intraday": 1, "swing": 10, "positional": 60}
_HORIZON_PERIOD: dict[Horizon, str] = {"intraday": "6mo", "swing": "2y", "positional": "5y"}
_MIN_LOOKBACK: dict[Horizon, int] = {"intraday": 80, "swing": 100, "positional": 180}


def calibration_universe(name: UniverseName = "nifty100", limit: int | None = None) -> list[str]:
    """Return a deterministic free-data universe from the bundled symbol list."""
    symbols = [s["symbol"] for s in MAJOR_STOCKS if not s["symbol"].startswith("^")]
    target = {"nifty50": 50, "nifty100": 100, "nifty500": len(symbols), "curated": len(symbols)}[name]
    if limit is not None:
        target = min(target, max(1, limit))
    return symbols[:target]


def _sector_for(symbol: str) -> str:
    for s in MAJOR_STOCKS:
        if s["symbol"] == symbol:
            return s.get("sector", "N/A")
    return "N/A"


def _weekly_technicals(df: pd.DataFrame) -> dict[str, Any] | None:
    try:
        wdf = df.resample("W").agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
        ).dropna()
        if len(wdf) >= 12:
            return compute_technicals(wdf)
    except Exception:
        return None
    return None


def _evaluate_trade(
    action: str,
    entry: float,
    stoploss: float,
    target: float,
    future: pd.DataFrame,
) -> dict[str, Any] | None:
    if future.empty or entry <= 0:
        return None
    max_fav = 0.0
    max_adv = 0.0
    exit_price = float(future["Close"].iloc[-1])
    outcome = "expired"
    reason = "time_expired"
    bars_held = len(future)

    for idx, (_ts, row) in enumerate(future.iterrows(), start=1):
        hi = float(row.get("High") or row.get("Close"))
        lo = float(row.get("Low") or row.get("Close"))
        if action == "BUY":
            max_fav = max(max_fav, (hi - entry) / entry * 100.0)
            max_adv = min(max_adv, (lo - entry) / entry * 100.0)
            if lo <= stoploss:
                outcome, reason, exit_price, bars_held = "loss", "stoploss_hit", stoploss, idx
                break
            if hi >= target:
                outcome, reason, exit_price, bars_held = "win", "target_hit", target, idx
                break
        else:
            max_fav = max(max_fav, (entry - lo) / entry * 100.0)
            max_adv = min(max_adv, (entry - hi) / entry * 100.0)
            if hi >= stoploss:
                outcome, reason, exit_price, bars_held = "loss", "stoploss_hit", stoploss, idx
                break
            if lo <= target:
                outcome, reason, exit_price, bars_held = "win", "target_hit", target, idx
                break

    pnl = (exit_price - entry) / entry * 100.0
    if action == "SELL":
        pnl = -pnl
    pnl -= TRANSACTION_COST_PCT
    return {
        "outcome": outcome,
        "outcome_reason": reason,
        "exit_price": round(exit_price, 2),
        "pnl_pct": round(pnl, 4),
        "max_favorable_pct": round(max_fav, 4),
        "max_adverse_pct": round(max_adv, 4),
        "bars_held": bars_held,
    }


def _record_from_slice(
    symbol: str,
    horizon: Horizon,
    df: pd.DataFrame,
    idx: int,
    min_conviction: int = 0,
) -> dict[str, Any] | None:
    eval_days = _HORIZON_DAYS[horizon]
    window = df.iloc[: idx + 1].copy()
    future = df.iloc[idx + 1: idx + 1 + eval_days].copy()
    if len(future) < max(1, min(eval_days, 5)):
        return None

    tech = compute_technicals(window)
    if not tech or not tech.get("current_price"):
        return None
    weekly = _weekly_technicals(window) if horizon != "intraday" else None
    regime = _market_regime(None, weekly)
    weights = _select_weights(None, regime)
    contributions, _fno, _fii = _score_all(
        tech,
        delivery_pct=None,
        fii_dii={},
        options=None,
        rs_rank=None,
        pchg_1d=0.0,
        news=[],
        fundamentals=None,
        weekly_tech=weekly,
        weights=weights,
        use_learned_edge=False,
    )
    weighted = sum(c.score * c.weight for c in contributions)
    entry = float(tech["current_price"])
    direction_up = weighted >= 0
    stoploss, target = None, None
    e, sl, t1, _t2 = entry_sl_targets(entry, tech.get("atr"), horizon, direction_up)
    rr = abs(t1 - e) / max(0.01, abs(e - sl))
    conviction, agreement, _note = calibrated_conviction(
        weighted, contributions, risk_reward=rr, regime=regime,
    )
    action = action_from_score(weighted, regime=regime)
    if action in ("BUY", "SELL") and conviction < min_conviction:
        return None
    if action not in ("BUY", "SELL"):
        return None
    stoploss, target = float(sl), float(t1)
    outcome = _evaluate_trade(action, e, stoploss, target, future)
    if not outcome:
        return None

    ts = df.index[idx]
    return {
        "symbol": symbol,
        "sector": _sector_for(symbol),
        "horizon": horizon,
        "action": action,
        "bar_index": idx,
        "entry_time": str(ts),
        "entry": round(e, 2),
        "stoploss": round(stoploss, 2),
        "target1": round(target, 2),
        "risk_reward": round(rr, 3),
        "conviction": conviction,
        "weighted_score": round(weighted, 4),
        "factor_agreement": agreement,
        "regime": regime,
        "signals": [
            {"name": c.name, "weight": c.weight, "score": c.score, "direction": c.direction}
            for c in contributions
        ],
        **outcome,
    }


async def _backtest_symbol(
    symbol: str,
    horizons: list[Horizon],
    *,
    period: str,
    stride: int,
    min_conviction: int,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    try:
        df = await async_fetch_history(symbol, period=period, interval="1d")
    except Exception as exc:
        return [], {"symbol": symbol, "error": str(exc)}
    if df is None or df.empty:
        return [], {"symbol": symbol, "error": "no_history"}

    records: list[dict[str, Any]] = []
    for horizon in horizons:
        eval_days = _HORIZON_DAYS[horizon]
        start = max(_MIN_LOOKBACK[horizon], 30)
        stop = len(df) - eval_days - 1
        if stop <= start:
            continue
        for idx in range(start, stop, max(1, stride)):
            rec = _record_from_slice(symbol, horizon, df, idx, min_conviction=min_conviction)
            if rec:
                records.append(rec)
    return records, None


def _aggregate(records: list[dict[str, Any]], key: str) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        buckets.setdefault(str(r.get(key) or "N/A"), []).append(r)
    out: dict[str, Any] = {}
    for name, items in buckets.items():
        wins = sum(1 for r in items if r["outcome"] == "win")
        losses = sum(1 for r in items if r["outcome"] == "loss")
        resolved = wins + losses
        out[name] = {
            "total": len(items),
            "wins": wins,
            "losses": losses,
            "expired": sum(1 for r in items if r["outcome"] == "expired"),
            "win_rate": round(wins / resolved * 100, 2) if resolved else 0.0,
            "avg_pnl_pct": round(sum(r["pnl_pct"] for r in items) / len(items), 4),
        }
    return out


# FDR gate on factor-edge mining. A factor edge is applied as a live weight
# multiplier only if it survives Benjamini-Hochberg across the whole family of
# factor×context tests AND has at least this many aligned samples.
_FACTOR_EDGE_FDR_ALPHA = 0.10
_MIN_EDGE_SAMPLES = 20


def _edge_pvalue(vals: list[float], overall_avg: float) -> float:
    """Two-sided p-value that mean(vals) differs from `overall_avg`.

    One-sample t-statistic on d_i = val_i − overall_avg, with a normal
    approximation to the CDF (no scipy dependency). Returns 1.0 (not
    significant) when the sample is too small or has zero variance, so a
    thin or degenerate bucket can never be promoted by the FDR gate.
    """
    n = len(vals)
    if n < _MIN_EDGE_SAMPLES:
        return 1.0
    diffs = [v - overall_avg for v in vals]
    mean_d = sum(diffs) / n
    var = sum((d - mean_d) ** 2 for d in diffs) / (n - 1)
    if var <= 0.0:
        return 1.0
    se = math.sqrt(var / n)
    if se <= 0.0:
        return 1.0
    t = mean_d / se
    # Two-sided p from the standard normal (large-n approximation of t).
    cdf = 0.5 * (1.0 + math.erf(abs(t) / math.sqrt(2.0)))
    return max(0.0, min(1.0, 2.0 * (1.0 - cdf)))


def _factor_edges(
    records: list[dict[str, Any]],
    context: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    if not records:
        return []
    context = context or {}
    suffix = "".join(f"|{key}={value}" for key, value in context.items())
    overall_avg = sum(r["pnl_pct"] for r in records) / len(records)
    aligned: dict[str, list[float]] = {}
    for r in records:
        direction = 1 if r["action"] == "BUY" else -1
        for sig in r.get("signals", []):
            score = float(sig.get("score") or 0.0)
            if abs(score) < 0.3 or score * direction <= 0:
                continue
            aligned.setdefault(f"{sig['name']}{suffix}", []).append(float(r["pnl_pct"]))

    rows: list[dict[str, Any]] = []
    for factor, vals in aligned.items():
        avg = sum(vals) / len(vals)
        rows.append(
            {
                "factor": factor,
                "total_directional": len(records),
                "aligned_count": len(vals),
                "aligned_avg_pnl": round(avg, 4),
                "overall_avg_pnl": round(overall_avg, 4),
                "edge": round(avg - overall_avg, 4),
                # Two-sided significance that the aligned subset's mean P&L
                # differs from the overall mean. Consumed by the FDR gate in
                # `_persist_run` — a factor edge is NOT applied as a weight
                # multiplier unless it survives Benjamini-Hochberg across the
                # whole family of factor×context tests. Ranking dozens of
                # buckets by raw edge and applying the top ones is a textbook
                # multiple-comparisons trap; this closes it.
                "p_value": round(_edge_pvalue(vals, overall_avg), 6),
            }
        )
    rows.sort(key=lambda r: r["edge"], reverse=True)
    return rows


def _contextual_factor_edges(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Factor edge split by regime, sector, horizon and key combinations."""
    rows: list[dict[str, Any]] = []

    def add_group(field: str, value: str, items: list[dict[str, Any]]) -> None:
        if len(items) < 20:
            return
        rows.extend(_factor_edges(items, {field: value}))

    for field in ("regime", "sector", "horizon"):
        buckets: dict[str, list[dict[str, Any]]] = {}
        for rec in records:
            buckets.setdefault(str(rec.get(field) or "N/A"), []).append(rec)
        for value, items in buckets.items():
            add_group(field, value, items)

    for first, second in (("regime", "horizon"), ("sector", "horizon")):
        buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for rec in records:
            key = (str(rec.get(first) or "N/A"), str(rec.get(second) or "N/A"))
            buckets.setdefault(key, []).append(rec)
        for (v1, v2), items in buckets.items():
            if len(items) < 20:
                continue
            rows.extend(_factor_edges(items, {first: v1, second: v2}))

    rows.sort(key=lambda r: r["edge"], reverse=True)
    return rows


async def _persist_run(payload: dict[str, Any], apply: bool) -> None:
    now = datetime.now(timezone.utc).isoformat()
    summary = payload["summary"]
    best = payload["factor_edges"][0]["factor"] if payload["factor_edges"] else None
    worst = payload["factor_edges"][-1]["factor"] if payload["factor_edges"] else None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO backtest_runs
               (run_at, period, eval_window_days, stocks_count, total_signals,
                avg_pnl_pct, directional_win_rate, best_signal_type, worst_signal_type, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now,
                payload["period"],
                max(payload["eval_windows"]),
                payload["symbols_requested"],
                summary["total"],
                summary["avg_pnl_pct"],
                summary["win_rate"],
                best,
                worst,
                json.dumps(payload),
            ),
        )
        if apply:
            # FDR gate: only factor edges that survive Benjamini-Hochberg
            # across the *whole family* of factor×context tests are persisted
            # and thus allowed to move a live weight multiplier. Without this,
            # ranking dozens of buckets by raw edge and applying the top ones
            # is a multiple-comparisons trap — the exact failure the FDR module
            # was built to prevent. Rows are annotated `significant` in the
            # payload (for transparency) but only significant ones are written.
            combined = [*payload["factor_edges"], *payload.get("contextual_factor_edges", [])]
            pvals = [float(r.get("p_value", 1.0)) for r in combined]
            keep = benjamini_hochberg(pvals, alpha=_FACTOR_EDGE_FDR_ALPHA)
            n_kept = 0
            for row, is_sig in zip(combined, keep):
                row["significant"] = bool(is_sig)
                if not is_sig:
                    continue
                n_kept += 1
                await db.execute(
                    """INSERT OR REPLACE INTO factor_performance
                       (factor, total_directional, aligned_count, aligned_avg_pnl,
                        overall_avg_pnl, edge, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row["factor"], row["total_directional"], row["aligned_count"],
                        row["aligned_avg_pnl"], row["overall_avg_pnl"], row["edge"], now,
                    ),
                )
            logger.info(
                "factor-edge FDR gate: %d/%d edges significant at alpha=%.2f (persisted); "
                "%d suppressed as multiple-comparisons noise",
                n_kept, len(combined), _FACTOR_EDGE_FDR_ALPHA, len(combined) - n_kept,
            )
        await db.commit()
    if apply:
        await seed_factor_edge_cache()


async def run_large_scale_calibration(
    *,
    universe: UniverseName = "nifty100",
    horizons: list[Horizon] | None = None,
    period: str | None = None,
    max_symbols: int | None = None,
    stride: int = 5,
    concurrency: int = 3,
    min_conviction: int = 0,
    apply: bool = True,
) -> dict[str, Any]:
    """Run large-scale calibration and optionally apply factor edges."""
    horizons = horizons or ["swing", "positional"]
    period = period or max((_HORIZON_PERIOD[h] for h in horizons), key=lambda p: {"6mo": 1, "1y": 2, "2y": 3, "5y": 4}.get(p, 0))
    symbols = calibration_universe(universe, max_symbols)
    sem = asyncio.Semaphore(max(1, min(concurrency, 10)))
    errors: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []

    async def one(sym: str) -> None:
        async with sem:
            recs, err = await _backtest_symbol(
                sym, horizons, period=period, stride=stride, min_conviction=min_conviction,
            )
            records.extend(recs)
            if err:
                errors.append(err)

    await asyncio.gather(*(one(sym) for sym in symbols))

    wins = sum(1 for r in records if r["outcome"] == "win")
    losses = sum(1 for r in records if r["outcome"] == "loss")
    resolved = wins + losses
    summary = {
        "total": len(records),
        "wins": wins,
        "losses": losses,
        "expired": sum(1 for r in records if r["outcome"] == "expired"),
        "win_rate": round(wins / resolved * 100, 2) if resolved else 0.0,
        "avg_pnl_pct": round(sum(r["pnl_pct"] for r in records) / len(records), 4) if records else 0.0,
    }
    payload = {
        "kind": "recommendation_calibration",
        "universe": universe,
        "symbols_requested": len(symbols),
        "symbols_available_in_repo": len(calibration_universe("curated")),
        "horizons": horizons,
        "period": period,
        "eval_windows": [_HORIZON_DAYS[h] for h in horizons],
        "stride": stride,
        "min_conviction": min_conviction,
        "transaction_cost_pct": TRANSACTION_COST_PCT,
        "summary": summary,
        "by_sector": _aggregate(records, "sector"),
        "by_regime": _aggregate(records, "regime"),
        "by_horizon": _aggregate(records, "horizon"),
        "factor_edges": _factor_edges(records),
        "contextual_factor_edges": _contextual_factor_edges(records),
        "sample_records": records[:50],
        "errors": errors,
        "methodology": {
            "walk_forward": True,
            "entry": "close_of_signal_bar",
            "exit": "first_stoploss_or_target_hit_else_horizon_close",
            "free_data_sources": "NSE daily history first, yfinance fallback",
            "note": "Nifty500 uses all bundled curated symbols unless a larger verified universe is added.",
        },
    }
    await _persist_run(payload, apply=apply)
    logger.info("Recommendation calibration complete: %s", summary)
    return payload

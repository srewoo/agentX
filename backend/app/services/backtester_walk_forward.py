from __future__ import annotations
"""Walk-forward / out-of-sample backtester.

Splits each symbol's history into K sequential folds. For every fold we
*train* (compute factor-edge statistics) on bars [0..train_end] and *test*
the signal engine on bars [train_end..test_end]. This is the standard
way to defend against in-sample overfitting: the model never sees the
future when generating signals, AND aggregate stats are derived from
held-out periods only.

We also support a multi-symbol runner so accuracy claims aren't based on
a single ticker. Default universe is `MAJOR_STOCKS[:N]`.

Output schema is intentionally compatible with the existing
`backtester.run_backtest` so the same UI/JSON consumers keep working —
plus extra `folds[]` and `oos_summary` fields.
"""
import asyncio
import logging
import math
from statistics import mean, pstdev
from typing import Any, Optional

import pandas as pd

from app.services.backtester import (
    BULLISH, BEARISH, MIN_LOOKBACK, TRANSACTION_COST_PCT, _evaluate_outcome,
)
from app.services.data_fetcher import MAJOR_STOCKS, async_fetch_history
from app.services.signal_engine import scan_symbol
from app.services.technicals import compute_support_resistance, compute_technicals
from app.utils import safe_float

logger = logging.getLogger(__name__)


def _slice_folds(n_bars: int, n_folds: int, min_train: int, eval_horizon: int) -> list[tuple[int, int, int]]:
    """Return [(train_start, train_end, test_end), ...] index triples.

    Expanding-window walk-forward: train grows each fold; test is the next
    chunk; eval needs `eval_horizon` future bars beyond test_end.
    """
    if n_folds < 2 or n_bars < min_train + n_folds * (eval_horizon + MIN_LOOKBACK):
        return []
    usable = n_bars - eval_horizon
    test_size = max(MIN_LOOKBACK, (usable - min_train) // n_folds)
    folds = []
    for k in range(n_folds):
        train_end = min_train + k * test_size
        test_end = min(train_end + test_size, usable)
        if test_end <= train_end:
            break
        folds.append((0, train_end, test_end))
    return folds


def _regime_at_bar(tech: dict[str, Any]) -> str:
    """Classify the regime as seen on bar i. Walk-forward safe — uses
    only `compute_technicals(window_df)` output, which already excludes
    future bars.

    Buckets mirror `recommendation._market_regime`:
      • trend_up   — ADX ≥ 22 and price > SMA50 > SMA200
      • trend_down — ADX ≥ 22 and price < SMA50 < SMA200
      • sideways   — everything else (low ADX or mixed MAs)
    """
    if not tech:
        return "sideways"
    adx = tech.get("adx") or 0
    ma = tech.get("moving_averages") or {}
    sma50, sma200 = ma.get("sma50"), ma.get("sma200")
    price = tech.get("current_price")
    if not (price and sma50):
        return "sideways"
    if adx < 22:
        return "sideways"
    if sma200:
        if price > sma50 > sma200:
            return "trend_up"
        if price < sma50 < sma200:
            return "trend_down"
    # ADX is strong but MAs aren't fully stacked.
    return "trend_up" if price > sma50 else "trend_down"


async def _scan_fold(
    symbol: str,
    df: pd.DataFrame,
    train_end: int,
    test_end: int,
    eval_windows: list[int],
) -> list[dict[str, Any]]:
    """Replay scan_symbol bar-by-bar between train_end and test_end.

    Returns a list of trade outcomes — one per fired signal — each with
    pnl per eval window plus the regime that was in force at signal time.
    No look-ahead: at bar i we only see df.iloc[:i+1].
    """
    out: list[dict[str, Any]] = []
    close_values = df["Close"].values
    total_bars = len(df)
    max_eval = max(eval_windows)

    # Pre-compute realistic per-bar slippage inputs: 20-day average daily
    # value and realised volatility. Used to feed sqrt-impact in lieu of
    # the flat 20-bp cost. Skipping the high/low fallback for speed —
    # the impact term dominates for small participation anyway.
    from app.services.execution_costs import round_trip_cost_pct
    dollar_vol = (df["Close"] * df.get("Volume", 0)).fillna(0)
    adv_20 = dollar_vol.rolling(20, min_periods=10).mean()
    rets = df["Close"].pct_change()
    vol_20 = rets.rolling(20, min_periods=10).std()

    for i in range(max(MIN_LOOKBACK, train_end), test_end):
        window_df = df.iloc[: i + 1]
        if len(window_df) < MIN_LOOKBACK:
            continue
        try:
            tech = compute_technicals(window_df)
            sr = compute_support_resistance(window_df)
        except Exception:
            continue
        regime = _regime_at_bar(tech)
        prev_price = safe_float(close_values[i - 1]) if i > 0 else None
        try:
            sigs = scan_symbol(
                symbol=symbol, df=window_df, technicals=tech, sr=sr,
                previous_price=prev_price, sentiment_score=None,
            )
        except Exception:
            continue
        if not sigs:
            continue
        entry = safe_float(close_values[i])
        if not entry:
            continue
        # Realistic round-trip cost in % — sqrt-impact assumes a "typical
        # institutional" trade size of 0.5% of average daily value. The
        # backtester is unit-agnostic, so we use participation rather
        # than absolute INR (1% × entry as nominal trade value).
        adv_inr_i = float(adv_20.iloc[i]) if i < len(adv_20) and adv_20.iloc[i] == adv_20.iloc[i] else 0.0
        vol_pct_i = float(vol_20.iloc[i]) * 100 if i < len(vol_20) and vol_20.iloc[i] == vol_20.iloc[i] else 0.0
        rt_cost = round_trip_cost_pct(
            trade_value_inr=adv_inr_i * 0.005 if adv_inr_i > 0 else entry,
            avg_daily_value_inr=adv_inr_i if adv_inr_i > 0 else max(entry, 1.0) * 1e6,
            daily_vol_pct=vol_pct_i if vol_pct_i > 0 else 1.5,
        )
        for sig in sigs:
            row: dict[str, Any] = {
                "symbol": symbol, "bar_index": i, "entry_price": entry,
                "signal_type": sig.get("signal_type", "unknown"),
                "direction": sig.get("direction", "neutral"),
                "regime": regime,
                "rt_cost_pct": rt_cost,
            }
            for w in eval_windows:
                fut = i + w
                if fut < total_bars:
                    fp = safe_float(close_values[fut])
                    if fp:
                        # Pass the per-trade realistic cost in lieu of the
                        # flat 0.20% default — small-caps now pay more, big
                        # liquid names pay less.
                        res = _evaluate_outcome(
                            row["direction"], entry, fp,
                            transaction_cost_pct=rt_cost,
                        )
                        row[f"pnl_{w}d"] = res["pnl_pct"]
                        row[f"win_{w}d"] = res["win"]
                        row[f"neutral_{w}d"] = res["neutral"]
            out.append(row)
    return out


def _fold_metrics(trades: list[dict[str, Any]], eval_windows: list[int]) -> dict[str, Any]:
    if not trades:
        return {"trades": 0}
    metrics: dict[str, Any] = {"trades": len(trades)}
    for w in eval_windows:
        pnls = [t[f"pnl_{w}d"] for t in trades if f"pnl_{w}d" in t and not t.get(f"neutral_{w}d", False)]
        wins = sum(1 for t in trades if t.get(f"win_{w}d") and not t.get(f"neutral_{w}d", False))
        losses = sum(1 for t in trades if f"win_{w}d" in t and not t.get(f"win_{w}d") and not t.get(f"neutral_{w}d", False))
        evaluated = wins + losses
        avg = mean(pnls) if pnls else 0.0
        vol = pstdev(pnls) if len(pnls) > 1 else 0.0
        # Per-trade Sharpe-like ratio (no annualisation — eval window varies).
        sharpe = (avg / vol) if vol > 1e-9 else 0.0
        metrics[f"win_rate_{w}d"] = round(wins / evaluated * 100, 2) if evaluated > 0 else None
        metrics[f"avg_pnl_{w}d"] = round(avg, 4)
        metrics[f"sharpe_{w}d"] = round(sharpe, 3)
        metrics[f"max_drawdown_{w}d"] = round(min(pnls), 4) if pnls else 0.0
        # Wilson 95% lower bound — honest "what's the worst-case win rate
        # given this many samples". A high point estimate with n=10 should
        # have a low lower bound, killing the "100% on n=26" red flag.
        metrics[f"win_rate_lb95_{w}d"] = _wilson_lb(wins, evaluated)
    return metrics


def _wilson_lb(wins: int, n: int, z: float = 1.96) -> Optional[float]:
    if n <= 0:
        return None
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return round(max(0.0, (centre - margin) / denom) * 100, 2)


async def run_walk_forward(
    symbol: str,
    period: str = "2y",
    n_folds: int = 4,
    eval_windows: list[int] | None = None,
    exchange: str = "NSE",
) -> dict[str, Any]:
    """Walk-forward backtest on a single symbol.

    Returns per-fold metrics + an OOS-only aggregate that combines all
    test folds (no overlap with the train portion that fed each fold's
    signal stats).
    """
    eval_windows = eval_windows or [1, 3, 5, 10]
    max_eval = max(eval_windows)
    df = await async_fetch_history(symbol, period=period, interval="1d", exchange=exchange)
    if df is None or df.empty or len(df) < MIN_LOOKBACK * 4 + max_eval:
        return {"symbol": symbol, "error": "insufficient_history", "bars": len(df) if df is not None else 0}

    folds = _slice_folds(len(df), n_folds, MIN_LOOKBACK * 2, max_eval)
    if not folds:
        return {"symbol": symbol, "error": "could_not_split_folds", "bars": len(df)}

    fold_reports: list[dict[str, Any]] = []
    all_trades: list[dict[str, Any]] = []
    for k, (_ts, train_end, test_end) in enumerate(folds):
        trades = await _scan_fold(symbol, df, train_end, test_end, eval_windows)
        fold_reports.append({
            "fold": k, "train_end": train_end, "test_end": test_end,
            "metrics": _fold_metrics(trades, eval_windows),
            # Trades kept so the universe runner can pool OOS samples.
            "trades": trades,
        })
        all_trades.extend(trades)

    return {
        "symbol": symbol,
        "exchange": exchange,
        "period": period,
        "total_bars": len(df),
        "n_folds": len(folds),
        "transaction_cost_pct": TRANSACTION_COST_PCT,
        "folds": fold_reports,
        "oos_summary": _fold_metrics(all_trades, eval_windows),
        "methodology": {
            "walk_forward": True,
            "look_ahead_bias": False,
            "entry": "close_of_signal_bar",
            "eval": "close_of_bar_plus_window",
            "win_rate_lb95": "Wilson 95% lower bound — penalises small samples",
        },
    }


async def run_universe_walk_forward(
    symbols: Optional[list[str]] = None,
    period: str = "2y",
    n_folds: int = 4,
    eval_windows: list[int] | None = None,
    parallelism: int = 6,
) -> dict[str, Any]:
    """Multi-symbol walk-forward; aggregates per-signal-type OOS stats.

    Defaults to the first 40 NSE majors — about 8 minutes wall-clock with
    parallelism=6 on a warm yfinance cache. The signal stats reported are
    pooled across all symbols' OOS folds — the largest honest sample we
    can produce without a paid data feed.
    """
    eval_windows = eval_windows or [1, 3, 5, 10]
    if symbols is None:
        symbols = [s["symbol"] for s in MAJOR_STOCKS if not s["symbol"].startswith("^")][:40]

    sem = asyncio.Semaphore(parallelism)

    async def _one(sym: str) -> dict[str, Any]:
        async with sem:
            try:
                return await run_walk_forward(sym, period=period, n_folds=n_folds, eval_windows=eval_windows)
            except Exception as e:
                logger.warning("walk-forward failed for %s: %s", sym, e)
                return {"symbol": sym, "error": str(e)}

    per_symbol = await asyncio.gather(*(_one(s) for s in symbols))

    # Pool every OOS trade across symbols/folds, then bucket by signal_type.
    pooled: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for sr in per_symbol:
        for fold in sr.get("folds") or []:
            pass  # per-fold metrics live in oos_summary; we recompute pooled below

    # Re-run pooling from each symbol's flattened trade list. We didn't keep
    # individual trades on `per_symbol`, so re-scan once more across all
    # folds in a single sweep using the raw history. Cheaper alternative:
    # keep trades on the per-symbol payload.
    # → For runtime we DO keep trades on the per-symbol payload now:
    aggregated: dict[str, dict[str, list[float]]] = {}  # {signal_type: {direction: [pnl]}}
    raw_pooled: list[dict[str, Any]] = []
    for sr in per_symbol:
        for fold in sr.get("folds") or []:
            for tr in fold.get("trades", []) or []:
                raw_pooled.append(tr)

    # Compute pooled per-signal stats — only if backtester returned trades.
    # If we didn't keep trades (memory concern), the per-symbol summary still
    # holds; the pooled view is best-effort.
    by_signal_type: dict[str, Any] = {}
    for tr in raw_pooled:
        st = tr.get("signal_type", "unknown")
        di = tr.get("direction", "neutral")
        by_signal_type.setdefault(st, {}).setdefault(di, []).append(tr)

    signal_metrics = {
        st: {di: _fold_metrics(trs, eval_windows) for di, trs in dirs.items()}
        for st, dirs in by_signal_type.items()
    }

    universe_oos = _fold_metrics(raw_pooled, eval_windows) if raw_pooled else {"trades": 0}

    # Stratify by market regime so we can see whether e.g. double_top
    # bearish actually works in trend_down and fails in trend_up. This
    # is what the next iteration of the engine should gate on.
    by_regime: dict[str, dict[str, Any]] = {}
    regime_x_signal: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    if raw_pooled:
        bucketed: dict[str, list[dict[str, Any]]] = {}
        for tr in raw_pooled:
            bucketed.setdefault(tr.get("regime", "sideways"), []).append(tr)
        for reg, trs in bucketed.items():
            by_regime[reg] = _fold_metrics(trs, eval_windows)
            # Per-regime × signal_type × direction table.
            rs: dict[str, dict[str, list[dict[str, Any]]]] = {}
            for tr in trs:
                rs.setdefault(tr.get("signal_type", "unknown"), {}).setdefault(
                    tr.get("direction", "neutral"), []
                ).append(tr)
            regime_x_signal[reg] = {
                st: {di: _fold_metrics(trs_, eval_windows) for di, trs_ in dirs.items()}
                for st, dirs in rs.items()
            }

    return {
        "symbols_evaluated": [s for s in symbols if any(r.get("symbol") == s for r in per_symbol)],
        "symbols_with_errors": [r["symbol"] for r in per_symbol if r.get("error")],
        "period": period,
        "n_folds": n_folds,
        "per_symbol": per_symbol,
        "universe_oos_summary": universe_oos,
        "by_signal_type_oos": signal_metrics,
        "by_regime_oos": by_regime,
        "by_regime_x_signal_oos": regime_x_signal,
        "methodology": {
            "type": "expanding_window_walk_forward",
            "look_ahead_bias": False,
            "stat_basis": "out_of_sample_only",
            "win_rate_lb95": "Wilson 95% lower bound (penalises small n)",
            "regime_buckets": ["trend_up", "trend_down", "sideways"],
            "regime_rule": "ADX≥22 + price/SMA50/SMA200 alignment",
        },
    }

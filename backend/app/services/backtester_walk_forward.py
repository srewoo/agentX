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
from typing import Any, Optional, Sequence

import pandas as pd

from app.services.backtester import (
    BULLISH, BEARISH, MIN_LOOKBACK, TRANSACTION_COST_PCT, _evaluate_outcome,
    _evaluate_outcome_realistic,
)
from app.services.data_fetcher import MAJOR_STOCKS, async_fetch_history
from app.services import holdout
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


def _simulate_path_exit(
    direction: str,
    entry: float,
    stop: float,
    target: float,
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    opens: Optional[Sequence[float]] = None,
) -> tuple[float, str, int]:
    """Simulate a live-style stop/target/time exit over a forward OHLC path.

    `highs`/`lows`/`closes` are the bars AFTER entry, chronological (i+1, i+2,
    ...). Returns ``(exit_price, exit_reason, bars_held)`` where exit_reason is
    "stop" | "target" | "time".

    This replaces the old fixed-horizon mark-to-close, which assumed every
    trade was held the full window with no stop/target — diverging sharply from
    the live engine (which exits at an ATR stop, a target, or a time barrier).
    A 1-3 day edge marked at close[i+w] looked profitable in backtest yet
    stopped out live; aligning the exit model is what closes that gap.

    **Gap-through slippage (2.5).** A stop is a stop-MARKET order: when a bar
    GAPS past the stop (opens beyond it), the real fill is at that open, not at
    the stop price. Modelling the fill exactly at the stop understates losses on
    gap days — the single biggest optimism left in the backtest exit. When
    ``opens`` is supplied, a bar whose open has already gapped through the stop
    fills at the open (worse); intrabar touches still fill at the stop. The
    favourable case (a bar gapping past the TARGET) is deliberately NOT credited
    the extra move — the target fills at the target price — so the model can
    only be pessimistic, never optimistic. With ``opens=None`` the legacy
    fill-at-stop behaviour is preserved.

    Daily bars hide intrabar order, so when one bar's range spans BOTH stop and
    target we assume the STOP filled first — the honest, risk-first convention
    (matches recommendation_calibration). If neither is touched across the whole
    path, exit at the final close (time-exit).
    """
    up = direction == "bullish"
    n = len(closes)
    for k in range(n):
        hi = float(highs[k]); lo = float(lows[k])
        o = float(opens[k]) if opens is not None and k < len(opens) else None
        if up:
            # Gap-down through the stop → fill at the (worse) open.
            if o is not None and o <= stop:
                return o, "stop", k + 1
            if lo <= stop:
                return stop, "stop", k + 1
            if hi >= target:
                return target, "target", k + 1
        else:  # bearish / short: stop is ABOVE entry, target BELOW
            # Gap-up through the stop → fill at the (worse) open.
            if o is not None and o >= stop:
                return o, "stop", k + 1
            if hi >= stop:
                return stop, "stop", k + 1
            if lo <= target:
                return target, "target", k + 1
    if n == 0:
        return entry, "time", 0
    return float(closes[-1]), "time", n


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
    # High/Low needed to simulate intra-window stop/target hits (item 8).
    high_values = df["High"].values if "High" in df.columns else close_values
    low_values = df["Low"].values if "Low" in df.columns else close_values
    # Open needed to model gap-through slippage on exits (2.5) — a bar that
    # gaps past the stop fills at the open, not at the stop price.
    open_values = df["Open"].values if "Open" in df.columns else close_values
    total_bars = len(df)
    max_eval = max(eval_windows)
    # Live-style ATR stop/target bands, so the backtest exit model matches the
    # engine instead of marking fixed-horizon close-to-close.
    from app.services.recommendation_factors import entry_sl_targets

    # Pre-compute realistic per-bar slippage inputs: 20-day average daily
    # value and realised volatility. Used to feed sqrt-impact in lieu of
    # the flat 20-bp cost. Skipping the high/low fallback for speed —
    # the impact term dominates for small participation anyway.
    from app.services.execution_costs import round_trip_cost_pct, sqrt_impact_cost_bps
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
        # 3.4 — apply the SAME earnings blackout the live scan uses, using the
        # point-in-time historical calendar. Removes the live/backtest mismatch
        # where the backtest traded through earnings the live engine sat out.
        # Inert (no skip) when no calendar data is present.
        try:
            from app.services.earnings_calendar_pit import is_in_blackout_at, has_calendar
            if has_calendar():
                bar_date = df.index[i]
                asof = bar_date.date() if hasattr(bar_date, "date") else bar_date
                if is_in_blackout_at(symbol, asof):
                    continue
        except Exception:
            pass
        entry = safe_float(close_values[i])
        if not entry:
            continue
        # Realistic round-trip cost in % — sqrt-impact assumes a "typical
        # institutional" trade size of 0.5% of average daily value. The
        # backtester is unit-agnostic, so we use participation rather
        # than absolute INR (1% × entry as nominal trade value).
        adv_inr_i = float(adv_20.iloc[i]) if i < len(adv_20) and adv_20.iloc[i] == adv_20.iloc[i] else 0.0
        vol_pct_i = float(vol_20.iloc[i]) * 100 if i < len(vol_20) and vol_20.iloc[i] == vol_20.iloc[i] else 0.0
        _trade_value_inr = adv_inr_i * 0.005 if adv_inr_i > 0 else entry
        _adv_value_inr = adv_inr_i if adv_inr_i > 0 else max(entry, 1.0) * 1e6
        _daily_vol_pct = vol_pct_i if vol_pct_i > 0 else 1.5
        rt_cost = round_trip_cost_pct(
            trade_value_inr=_trade_value_inr,
            avg_daily_value_inr=_adv_value_inr,
            daily_vol_pct=_daily_vol_pct,
        )
        # Size-aware market-impact component of the round-trip cost (round trip
        # = 2× one-way impact_bps). This is the piece apply_costs' flat
        # ADV-bucket slippage can't express; we feed it into the P&L below so
        # the sqrt-impact model actually reduces headline returns instead of
        # being computed and shelved.
        _impact_rt_pct = (
            sqrt_impact_cost_bps(
                trade_value_inr=_trade_value_inr,
                avg_daily_value_inr=_adv_value_inr,
                daily_vol_pct=_daily_vol_pct,
            )["impact_bps"] * 2 / 100.0
        )
        # Pre-compute engineered features the meta-judge can train on.
        # These are *legitimate-at-decision-time* (the engine sees them too
        # at scan time, we just didn't persist them before).
        sma20 = float(window_df["Close"].rolling(20).mean().iloc[-1]) if len(window_df) >= 20 else 0.0
        sma50 = float(window_df["Close"].rolling(50).mean().iloc[-1]) if len(window_df) >= 50 else 0.0
        sma200 = float(window_df["Close"].rolling(200).mean().iloc[-1]) if len(window_df) >= 200 else 0.0
        dist_sma20 = ((entry - sma20) / sma20 * 100.0) if sma20 else 0.0
        dist_sma50 = ((entry - sma50) / sma50 * 100.0) if sma50 else 0.0
        dist_sma200 = ((entry - sma200) / sma200 * 100.0) if sma200 else 0.0
        # 20-day return regime
        if len(window_df) >= 20:
            ret_20d = float((entry - close_values[i - 20]) / close_values[i - 20] * 100.0)
        else:
            ret_20d = 0.0
        rsi_now = float(tech.get("rsi") or 50.0) if isinstance(tech, dict) else 50.0
        atr_pct = (vol_pct_i if vol_pct_i > 0 else 1.5)
        atr_abs = float(tech.get("atr") or 0.0) if isinstance(tech, dict) else 0.0

        for sig in sigs:
            direction = sig.get("direction", "neutral")
            row: dict[str, Any] = {
                "symbol": symbol, "bar_index": i, "entry_price": entry,
                "signal_type": sig.get("signal_type", "unknown"),
                "direction": direction,
                "regime": regime,
                "rt_cost_pct": rt_cost,
                # Engineered features for the meta-judge.
                "strength": int(sig.get("strength", 5)),
                "dist_sma20_pct": round(dist_sma20, 2),
                "dist_sma50_pct": round(dist_sma50, 2),
                "dist_sma200_pct": round(dist_sma200, 2),
                "ret_20d_pct": round(ret_20d, 2),
                "rsi": round(rsi_now, 1),
                "atr_pct": round(atr_pct, 2),
            }
            # Live-style ATR stop/target for directional signals (item 8). Use
            # the same swing bands the recommendation engine applies; neutral
            # signals have no tradable direction so they keep mark-to-close.
            directional = direction in ("bullish", "bearish")
            if directional:
                _, stop_px, tgt_px, _ = entry_sl_targets(
                    entry, atr_abs or None, "swing", direction == "bullish")
            for w in eval_windows:
                fut = i + w
                if fut < total_bars:
                    if directional:
                        # Walk bars i+1..i+w; exit at stop/target if breached
                        # intra-window, else time-exit at close[i+w].
                        exit_px, exit_reason, bars_held = _simulate_path_exit(
                            direction, entry, stop_px, tgt_px,
                            high_values[i + 1: fut + 1],
                            low_values[i + 1: fut + 1],
                            close_values[i + 1: fut + 1],
                            open_values[i + 1: fut + 1],
                        )
                    else:
                        exit_px, exit_reason, bars_held = (
                            safe_float(close_values[fut]), "time", w)
                    if exit_px:
                        # Net-of-cost via apply_costs (brokerage + STT + DP +
                        # slippage). Falls back to the legacy flat-cost
                        # evaluator if any input is malformed.
                        try:
                            res = _evaluate_outcome_realistic(
                                direction, entry, exit_px,
                                qty=1,
                                segment="cash",
                                avg_daily_volume=adv_inr_i / max(entry, 1.0) if adv_inr_i else None,
                                extra_slippage_pct=_impact_rt_pct,
                            )
                        except Exception:
                            res = _evaluate_outcome(
                                direction, entry, exit_px,
                                transaction_cost_pct=rt_cost,
                            )
                        row[f"pnl_{w}d"] = res["pnl_pct"]
                        row[f"win_{w}d"] = res["win"]
                        row[f"neutral_{w}d"] = res["neutral"]
                        row[f"exit_reason_{w}d"] = exit_reason
                        row[f"bars_held_{w}d"] = bars_held
                        if "gross_pnl_pct" in res:
                            row[f"gross_pnl_{w}d"] = res["gross_pnl_pct"]
                            row[f"costs_pct_{w}d"] = res["costs_pct"]
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
        # Raw counts exposed so downstream (e.g. autonomous gating) can build
        # significance candidates from OOS wins/n directly, rather than
        # reconstructing them from the rounded win-rate.
        metrics[f"wins_{w}d"] = wins
        metrics[f"evaluated_{w}d"] = evaluated
        metrics[f"avg_pnl_{w}d"] = round(avg, 4)
        metrics[f"sharpe_{w}d"] = round(sharpe, 3)
        metrics[f"max_drawdown_{w}d"] = round(min(pnls), 4) if pnls else 0.0
        # Wilson 95% lower bound — honest "what's the worst-case win rate
        # given this many samples". A high point estimate with n=10 should
        # have a low lower bound, killing the "100% on n=26" red flag.
        metrics[f"win_rate_lb95_{w}d"] = _wilson_lb(wins, evaluated)

        # Monte Carlo over signal order — robustness check on the WR/Sharpe
        # point estimates. p5 < 45% suggests the strategy is fragile and
        # might be a lucky chronological draw rather than real edge.
        try:
            from app.services.execution_costs import monte_carlo_signal_order
            mc = monte_carlo_signal_order(pnls)
            metrics[f"mc_wr_p5_{w}d"] = mc.get("wr_p5")
            metrics[f"mc_wr_p50_{w}d"] = mc.get("wr_p50")
            metrics[f"mc_wr_p95_{w}d"] = mc.get("wr_p95")
            metrics[f"mc_sharpe_p5_{w}d"] = mc.get("sharpe_p5")
        except Exception:
            pass

        # Benchmark attribution — only present when NIFTY data resolved.
        excesses = [
            t[f"excess_pnl_{w}d"] for t in trades
            if f"excess_pnl_{w}d" in t and not t.get(f"neutral_{w}d", False)
        ]
        if excesses:
            benches = [
                t[f"bench_ret_{w}d"] for t in trades
                if f"bench_ret_{w}d" in t and not t.get(f"neutral_{w}d", False)
            ]
            metrics[f"excess_avg_pnl_{w}d"] = round(mean(excesses), 4)
            metrics[f"bench_avg_ret_{w}d"] = round(mean(benches), 4) if benches else None
            metrics[f"excess_positive_{w}d"] = round(
                sum(1 for e in excesses if e > 0) / len(excesses) * 100, 2)

        # Worst peak-to-trough equity drawdown across the chronological
        # sequence. Negative number; -0.25 = 25% drawdown.
        if pnls:
            equity = []
            cum = 0.0
            for p in pnls:
                cum += p / 100.0
                equity.append(cum)
            peak = equity[0]
            max_dd = 0.0
            for v in equity:
                peak = max(peak, v)
                dd = v - peak
                if dd < max_dd:
                    max_dd = dd
            metrics[f"max_dd_chronological_{w}d"] = round(max_dd, 4)
    return metrics


# ── Benchmark attribution (NIFTY) ────────────────────────────
# A long-biased book in a rising tape looks like alpha unless every trade is
# compared against simply holding the index over the same bars. Each trade
# gets `bench_ret_{w}d` (NIFTY return over its actual holding period) and
# `excess_pnl_{w}d` (direction-aware: a short's alpha is pnl PLUS the index
# move it was fighting). Fail-open: no benchmark data → no keys attached.
_BENCH_SYMBOL = "^NSEI"
_bench_cache: dict[str, tuple[float, Any]] = {}
_BENCH_TTL = 3600.0


async def _get_benchmark_closes(period: str):
    """NIFTY close series for `period`, memoized so a 40-symbol universe run
    doesn't fetch the same index history 40 times."""
    import time as _time
    hit = _bench_cache.get(period)
    if hit and (_time.time() - hit[0]) < _BENCH_TTL:
        return hit[1]
    try:
        bdf = await async_fetch_history(_BENCH_SYMBOL, period=period, interval="1d")
        closes = bdf["Close"] if bdf is not None and not bdf.empty else None
    except Exception:
        closes = None
    _bench_cache[period] = (_time.time(), closes)
    return closes


def _attach_benchmark(
    trades: list[dict[str, Any]],
    symbol_index,
    bench_closes,
    eval_windows: list[int],
) -> None:
    """Stamp per-trade benchmark + excess returns in place."""
    if bench_closes is None or not trades:
        return
    import pandas as pd
    bench_idx = pd.DatetimeIndex(pd.to_datetime(bench_closes.index)).tz_localize(None)
    sym_idx = pd.DatetimeIndex(pd.to_datetime(symbol_index)).tz_localize(None)
    bench_vals = bench_closes.values
    for t in trades:
        i = t.get("bar_index")
        if i is None or i >= len(sym_idx):
            continue
        pos = bench_idx.searchsorted(sym_idx[i])
        if pos >= len(bench_vals):
            continue
        entry_b = float(bench_vals[pos])
        if entry_b <= 0:
            continue
        for w in eval_windows:
            if f"pnl_{w}d" not in t:
                continue
            held = int(t.get(f"bars_held_{w}d") or w)
            exit_pos = min(pos + held, len(bench_vals) - 1)
            if exit_pos <= pos:
                continue
            bench_ret = (float(bench_vals[exit_pos]) - entry_b) / entry_b * 100.0
            t[f"bench_ret_{w}d"] = round(bench_ret, 4)
            sign = -1.0 if t.get("direction") == "bearish" else 1.0
            t[f"excess_pnl_{w}d"] = round(t[f"pnl_{w}d"] - sign * bench_ret, 4)


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
    referee: bool = False,
) -> dict[str, Any]:
    """Walk-forward backtest on a single symbol.

    Returns per-fold metrics + an OOS-only aggregate that combines all
    test folds (no overlap with the train portion that fed each fold's
    signal stats).

    This is a SELECTION path (it feeds the FDR gating loop), so by default it
    refuses to read past the pinned holdout boundary (1.2). ``referee=True`` is
    the deliberate, one-time escape hatch that reads the reserved window for the
    final out-of-sample verdict.
    """
    eval_windows = eval_windows or [1, 3, 5, 10]
    max_eval = max(eval_windows)
    df = await async_fetch_history(symbol, period=period, interval="1d", exchange=exchange)
    # 1.2 — quarantine the reserved holdout from selection.
    boundary = await holdout.resolve_boundary()
    df = holdout.trim_history(df, boundary, referee=referee)
    if df is None or df.empty or len(df) < MIN_LOOKBACK * 4 + max_eval:
        return {"symbol": symbol, "error": "insufficient_history", "bars": len(df) if df is not None else 0}

    folds = _slice_folds(len(df), n_folds, MIN_LOOKBACK * 2, max_eval)
    if not folds:
        return {"symbol": symbol, "error": "could_not_split_folds", "bars": len(df)}

    fold_trades: list[tuple[int, int, int, list[dict[str, Any]]]] = []
    all_trades: list[dict[str, Any]] = []
    for k, (_ts, train_end, test_end) in enumerate(folds):
        trades = await _scan_fold(symbol, df, train_end, test_end, eval_windows)
        fold_trades.append((k, train_end, test_end, trades))
        all_trades.extend(trades)

    # Benchmark attribution BEFORE metrics so excess-return aggregates land
    # in both the per-fold and pooled summaries. Skip for the index itself.
    if symbol != _BENCH_SYMBOL:
        bench_closes = await _get_benchmark_closes(period)
        _attach_benchmark(all_trades, df.index, bench_closes, eval_windows)

    fold_reports: list[dict[str, Any]] = []
    for k, train_end, test_end, trades in fold_trades:
        fold_reports.append({
            "fold": k, "train_end": train_end, "test_end": test_end,
            "metrics": _fold_metrics(trades, eval_windows),
            # Trades kept so the universe runner can pool OOS samples.
            "trades": trades,
        })

    return {
        "symbol": symbol,
        "exchange": exchange,
        "period": period,
        "total_bars": len(df),
        "n_folds": len(folds),
        # Trades are evaluated net of the FULL execution_costs model (brokerage
        # + STT + exchange + SEBI + DP + GST + slippage); the flat constant is
        # only the legacy fallback used when a cost input is malformed.
        "cost_model": "execution_costs.apply_costs",
        "transaction_cost_pct_fallback": TRANSACTION_COST_PCT,
        "folds": fold_reports,
        "oos_summary": _fold_metrics(all_trades, eval_windows),
        "methodology": {
            "walk_forward": True,
            "look_ahead_bias": False,
            "entry": "close_of_signal_bar",
            "eval": "close_of_bar_plus_window",
            "win_rate_lb95": "Wilson 95% lower bound — penalises small samples",
            "benchmark": (
                f"{_BENCH_SYMBOL} over each trade's actual holding period; "
                "excess_pnl is direction-aware (short alpha = pnl + index move)"
            ),
        },
    }


async def run_universe_walk_forward(
    symbols: Optional[list[str]] = None,
    period: str = "2y",
    n_folds: int = 4,
    eval_windows: list[int] | None = None,
    parallelism: int = 6,
    referee: bool = False,
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
                return await run_walk_forward(sym, period=period, n_folds=n_folds, eval_windows=eval_windows, referee=referee)
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

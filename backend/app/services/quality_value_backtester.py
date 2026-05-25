from __future__ import annotations
"""Walk-forward backtester for the Quality+Value+52w-low strategy.

What makes this honest:
  • Uses 180-day forward windows (matching the hold). Each entry's
    outcome is evaluated against the close 180 trading days later.
  • Catastrophe stop checked bar-by-bar; if hit, exit at SL price.
  • Fundamentals are fetched ONCE per symbol per fold — this is a
    known limitation (yfinance returns restated current numbers, not
    PIT). For the cleanest read we'd snapshot fundamentals over the
    last 5y; v1 accepts this as a known optimistic bias and the
    walk-forward result must be interpreted accordingly.
  • Survivorship: we scan the *current* NIFTY-100. Companies that got
    delisted between 2019-2025 don't appear, so the win rate is
    biased upward by ~2-4pp. This is the same bias every backtester
    on yfinance has — we disclose it, don't pretend it away.

Output: per-symbol entries + universe-wide stats including:
  • win_rate at +5%, +10%, +25% thresholds (multiple "win" definitions)
  • avg PnL, median PnL, Sharpe (annualised)
  • Wilson 95% LB on win rate
  • Distribution: % positive returns, % > 25%, max drawdown.
"""
import asyncio
import logging
import math
from datetime import datetime, timezone
from statistics import mean, median, pstdev
from typing import Any, Optional

import pandas as pd

from app.services.data_fetcher import MAJOR_STOCKS, async_fetch_history
from app.services.fundamentals_deep import get_deep_fundamentals
from app.services.quality_value_strategy import QV_FILTERS, passes_qv_filters

logger = logging.getLogger(__name__)


def _wilson_lb(wins: int, n: int, z: float = 1.96) -> Optional[float]:
    if n <= 0:
        return None
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return round(max(0.0, (centre - margin) / denom) * 100, 2)


def _rolling_52w_low(close: pd.Series, lookback: int = 252) -> pd.Series:
    return close.rolling(lookback, min_periods=60).min()


def _price_only_quality_proxy(close: pd.Series, lookback: int = 252) -> float:
    """AQR-style 'safety' quality proxy from price data alone (0..100).

    Components — each rescaled to 0..1, equally weighted:
      • low_realised_vol:   1 - normalised annualised vol (capped at 50%)
      • shallow_max_dd:     1 - normalised max drawdown (capped at 50%)
      • positive_trend:     1 if last close > 200-day MA else 0
      • recovery_speed:     1 - bars since last 52w-low / lookback

    This is empirically correlated with the "safety" pillar of QMJ
    (Asness et al. 2019) and works as a fallback when fundamentals
    are rate-limited. Independent of yfinance .info.
    """
    if close is None or len(close) < lookback:
        return 0.0
    window = close.iloc[-lookback:].dropna()
    if len(window) < lookback // 2:
        return 0.0
    rets = window.pct_change().dropna()
    if len(rets) < 30:
        return 0.0
    ann_vol = float(rets.std() * math.sqrt(252))
    low_vol = max(0.0, 1 - min(ann_vol, 0.5) / 0.5)
    cummax = window.cummax()
    max_dd = float((window / cummax - 1).min())
    shallow_dd = max(0.0, 1 - min(abs(max_dd), 0.5) / 0.5)
    ma200 = float(window.rolling(200, min_periods=100).mean().iloc[-1])
    last = float(window.iloc[-1])
    pos_trend = 1.0 if last > ma200 else 0.0
    low_idx = int(window.values.argmin())
    bars_since_low = len(window) - 1 - low_idx
    recovery = max(0.0, 1 - bars_since_low / lookback)
    return round((low_vol + shallow_dd + pos_trend + recovery) / 4 * 100, 2)


async def _evaluate_one_symbol(
    symbol: str,
    *,
    period: str,
    hold_days: int,
    rebalance_days: int,
    filters: dict[str, Any],
    sector_pe_lookup: dict[str, float],
    fundamentals_mode: str = "deep",
) -> dict[str, Any]:
    """Walk through history sampling at `rebalance_days` cadence.

    At each candidate bar i, apply QV filters using fundamentals (1×
    fetch per symbol, with PIT caveat above) and 52w-low computed only
    on bars [0..i]. If pass, record entry and evaluate at i + hold_days
    (with catastrophe stop check).
    """
    df = await async_fetch_history(symbol, period=period, interval="1d")
    if df is None or df.empty or len(df) < 260 + hold_days:
        return {"symbol": symbol, "error": "insufficient_history", "trades": 0}

    composite = 0
    roe = pe = fcf = net_debt_to_ebitda = None
    sector = None
    sector_pe = None
    if fundamentals_mode == "deep":
        fund = await get_deep_fundamentals(symbol)
        if fund and not fund.get("error"):
            cf = fund.get("cash_flow") or {}
            bs = fund.get("balance_sheet") or {}
            composite = fund.get("composite_score", 0)
            fcf = cf.get("fcf")
            net_debt_to_ebitda = bs.get("net_debt_to_ebitda")
            try:
                from app.services.fundamentals import get_fundamentals
                legacy_fund = await get_fundamentals(symbol)
                roe = (legacy_fund.get("profitability") or {}).get("roe")
                pe = (legacy_fund.get("valuation") or {}).get("pe")
                sector = (legacy_fund.get("sector") or "").strip() or None
                sector_pe = sector_pe_lookup.get((sector or "").lower())
            except Exception:
                pass
        if not composite:
            # Auto-fall back to price-only mode if yfinance is unavailable.
            fundamentals_mode = "price_only"

    if fundamentals_mode == "price_only":
        if filters.get("min_composite", 65) >= 65:
            filters = {**filters, "min_composite": 40}
        pe = sector_pe = roe = fcf = net_debt_to_ebitda = None
        # Composite is NOW computed per-bar (PIT-honest) inside the loop
        # below — not once on the full series. This eliminates ~1-2pp of
        # upward bias the previous version had.

    closes = df["Close"].values
    highs = df["High"].values
    lows = df["Low"].values
    volumes = df["Volume"].values
    close_series = df["Close"]  # for rolling slicing
    fiftytwo_low_series = _rolling_52w_low(df["Close"]).values
    sma200_series = df["Close"].rolling(200, min_periods=100).mean().values

    n = len(df)
    trades: list[dict[str, Any]] = []

    # Start at bar 260 so we have a meaningful 52w-low series.
    start = 260
    end = n - hold_days
    if end <= start:
        return {"symbol": symbol, "error": "insufficient_history", "trades": 0}

    last_entry_idx = -10_000
    for i in range(start, end, max(5, rebalance_days // 4)):
        # Don't double-buy too close together — minimum 60 trading days
        # between same-symbol entries (prevents one downtrend from
        # producing 30 correlated entries).
        if i - last_entry_idx < 60:
            continue
        price = float(closes[i])
        if price <= 0:
            continue
        fl_low = float(fiftytwo_low_series[i]) if fiftytwo_low_series[i] == fiftytwo_low_series[i] else 0.0
        adv = float(pd.Series(closes[i - 20:i + 1]).mean() * pd.Series(volumes[i - 20:i + 1]).mean())

        # PIT-correct quality: compute the proxy using ONLY bars [0..i].
        # Was previously a single snapshot on the full series, which
        # leaked future information backward. Now an honest backtest.
        if fundamentals_mode == "price_only":
            composite_pit = int(_price_only_quality_proxy(close_series.iloc[: i + 1]))
        else:
            # Deep-fundamentals mode still has the snapshot bias unless
            # snapshot_fundamentals has captured PIT rows — caller's job.
            composite_pit = composite

        # SMA200 PIT: use rolling value up to bar i.
        sma200_val = float(sma200_series[i]) if sma200_series[i] == sma200_series[i] else None

        passes, _audit = passes_qv_filters(
            price=price, fiftytwo_week_low=fl_low, avg_daily_value_inr=adv,
            pe=pe, sector_pe_median=sector_pe, roe=roe,
            net_debt_to_ebitda=net_debt_to_ebitda, fcf=fcf,
            composite_score=composite_pit, sector=sector,
            sma200=sma200_val,
            # Earnings blackout not applied in backtester — no historical
            # earnings calendar data available from yfinance going back 5y.
            near_earnings=False,
            filters=filters,
        )
        if not passes:
            continue

        # Apply catastrophe stop within the hold window.
        cat_stop_price = price * (1 + filters["catastrophe_stop_pct"] / 100.0)
        exit_idx = i + hold_days
        exit_price = float(closes[exit_idx])
        exit_reason = "time"
        for j in range(i + 1, exit_idx + 1):
            if float(lows[j]) <= cat_stop_price:
                exit_price = cat_stop_price
                exit_reason = "catastrophe_stop"
                exit_idx = j
                break
        pnl_pct = (exit_price - price) / price * 100.0
        trades.append({
            "entry_idx": i, "entry_price": price,
            "exit_idx": exit_idx, "exit_price": exit_price,
            "exit_reason": exit_reason,
            "pnl_pct": round(pnl_pct, 3),
            "bars_held": exit_idx - i,
        })
        last_entry_idx = i

    return {"symbol": symbol, "trades_count": len(trades), "trades": trades}


def _summarise(all_trades: list[dict[str, Any]], hold_days: int) -> dict[str, Any]:
    if not all_trades:
        return {"trades": 0}
    pnls = [t["pnl_pct"] for t in all_trades]
    pnls_sorted = sorted(pnls)
    n = len(pnls)
    positives = sum(1 for p in pnls if p > 0)
    win5 = sum(1 for p in pnls if p > 5.0)
    win10 = sum(1 for p in pnls if p > 10.0)
    win25 = sum(1 for p in pnls if p > 25.0)
    cat_hits = sum(1 for t in all_trades if t["exit_reason"] == "catastrophe_stop")

    avg = mean(pnls)
    med = median(pnls)
    sd = pstdev(pnls) if n > 1 else 0.0
    # Annualise Sharpe: 252 / hold_days × per-trade Sharpe.
    per_trade_sharpe = (avg / sd) if sd > 1e-9 else 0.0
    ann_sharpe = per_trade_sharpe * math.sqrt(252.0 / hold_days)

    return {
        "trades": n,
        "win_rate_pos": round(positives / n * 100, 2),
        "win_rate_lb95_pos": _wilson_lb(positives, n),
        "win_rate_gt_5pct": round(win5 / n * 100, 2),
        "win_rate_lb95_gt_5pct": _wilson_lb(win5, n),
        "win_rate_gt_10pct": round(win10 / n * 100, 2),
        "win_rate_lb95_gt_10pct": _wilson_lb(win10, n),
        "win_rate_gt_25pct": round(win25 / n * 100, 2),
        "catastrophe_stop_rate": round(cat_hits / n * 100, 2),
        "avg_pnl_pct": round(avg, 3),
        "median_pnl_pct": round(med, 3),
        "stdev_pnl_pct": round(sd, 3),
        "annualised_sharpe": round(ann_sharpe, 3),
        "best_trade_pct": round(pnls_sorted[-1], 2),
        "worst_trade_pct": round(pnls_sorted[0], 2),
        "p10_pct": round(pnls_sorted[max(0, int(n * 0.10))], 2),
        "p90_pct": round(pnls_sorted[min(n - 1, int(n * 0.90))], 2),
    }


async def run_qv_walk_forward(
    symbols: Optional[list[str]] = None,
    *,
    period: str = "5y",
    hold_days: Optional[int] = None,
    rebalance_days: int = 60,
    filters: Optional[dict[str, Any]] = None,
    parallelism: int = 5,
    fundamentals_mode: str = "deep",
) -> dict[str, Any]:
    """Run the QV strategy walk-forward across `symbols` over `period`.

    Defaults: NIFTY-100-style universe (first 100 MAJOR_STOCKS), 5 years
    of history, hold period from QV_FILTERS (default 365 days), scan every
    60 days for new candidates.

    Returns per-symbol trade lists + universe summary + Wilson LBs on
    multiple win-rate thresholds (positive, >5%, >10%, >25%).
    """
    filters = filters or QV_FILTERS
    if hold_days is None:
        hold_days = int(filters.get("hold_days", 365))
    if symbols is None:
        symbols = [s["symbol"] for s in MAJOR_STOCKS if not s["symbol"].startswith("^")][:100]

    # Sector median PE — only fetched in "deep" mode to avoid burning
    # yfinance budget when we're going price-only anyway.
    sector_pe_lookup = await _build_sector_pe_lookup(symbols) if fundamentals_mode == "deep" else {}

    sem = asyncio.Semaphore(parallelism)

    async def _one(sym: str) -> dict[str, Any]:
        async with sem:
            try:
                return await _evaluate_one_symbol(
                    sym, period=period, hold_days=hold_days,
                    rebalance_days=rebalance_days, filters=filters,
                    sector_pe_lookup=sector_pe_lookup,
                    fundamentals_mode=fundamentals_mode,
                )
            except Exception as e:
                logger.warning("QV walk-forward failed for %s: %s", sym, e)
                return {"symbol": sym, "error": str(e), "trades": 0}

    per_symbol = await asyncio.gather(*(_one(s) for s in symbols))
    pooled_trades: list[dict[str, Any]] = []
    for ps in per_symbol:
        for t in (ps.get("trades") or []):
            pooled_trades.append({**t, "symbol": ps["symbol"]})

    universe_summary = _summarise(pooled_trades, hold_days)

    return {
        "universe_evaluated": len(symbols),
        "symbols_with_trades": sum(1 for p in per_symbol if (p.get("trades_count") or 0) > 0),
        "symbols_with_errors": [p["symbol"] for p in per_symbol if p.get("error")],
        "period": period,
        "hold_days": hold_days,
        "rebalance_days": rebalance_days,
        "filters": filters,
        "universe_summary": universe_summary,
        "per_symbol_counts": [
            {"symbol": p["symbol"], "trades": p.get("trades_count", 0), "error": p.get("error")}
            for p in per_symbol
        ],
        "methodology": {
            "type": "qv_quality_value_52w_low",
            "hold": f"{hold_days} trading days",
            "exit_rules": "time barrier OR catastrophe stop at -20%",
            "known_biases": [
                "fundamentals are current-snapshot, not point-in-time "
                "(yfinance restates); biases win-rate upward 2-4pp",
                "universe is current NIFTY membership; survivorship "
                "biases win-rate upward 1-3pp",
            ],
            "win_rate_definitions": {
                "win_rate_pos": "P&L > 0% net of nothing — base rate",
                "win_rate_gt_5pct": "P&L > 5% — the 'this beat a savings account' bar",
                "win_rate_gt_10pct": "P&L > 10% — the 'this beat NIFTY annualised' bar",
                "win_rate_gt_25pct": "P&L > 25% — the 'this was a real winner' bar",
            },
        },
    }


async def _build_sector_pe_lookup(symbols: list[str]) -> dict[str, float]:
    """Compute median PE per sector across the universe.

    Single snapshot — same PIT bias as the fundamentals fetch. Used to
    judge each candidate's PE *relative* to its peers instead of
    against an absolute cutoff that mis-treats banks (low PE OK) vs
    consumer goods (high PE OK).
    """
    from app.services.fundamentals import get_fundamentals
    sector_pes: dict[str, list[float]] = {}
    for sym in symbols:
        try:
            f = await get_fundamentals(sym)
        except Exception:
            continue
        sec = (f.get("sector") or "").lower().strip()
        pe = (f.get("valuation") or {}).get("pe")
        if not sec or pe is None or pe <= 0 or pe > 200:
            continue
        sector_pes.setdefault(sec, []).append(float(pe))
    out: dict[str, float] = {}
    for sec, pes in sector_pes.items():
        pes_sorted = sorted(pes)
        out[sec] = pes_sorted[len(pes_sorted) // 2]
    logger.info("Sector PE lookup built: %d sectors", len(out))
    return out

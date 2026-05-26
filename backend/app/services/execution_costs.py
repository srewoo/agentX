from __future__ import annotations
"""Realistic execution-cost models (Almgren-Chriss square-root market impact)
and point-in-time (PIT) fundamentals snapshotting.

Two problems with the prior 20-bp flat transaction cost:

  • It's optimistic for small-caps where a 1% ADV order can cost 40-60 bp
    in market impact alone (Almgren et al. 2005, "Direct Estimation of
    Equity Market Impact").
  • It's pessimistic for the most liquid NIFTY names where round-trip
    after broker rebates can be 8-12 bp.

We add a per-trade cost model and a fundamentals snapshot table so the
walk-forward can't accidentally use *restated* numbers from the future.
"""
import logging
import math
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite

from app.database import DB_PATH

logger = logging.getLogger(__name__)


# Indian-market constants. Round-trip = brokerage + STT + SEBI + stamp.
_FIXED_FEES_BPS = 12.0  # ~0.12% combined fixed cost (broker + statutory)


def sqrt_impact_cost_bps(
    *,
    trade_value_inr: float,
    avg_daily_value_inr: float,
    daily_vol_pct: float,
    fixed_fees_bps: float = _FIXED_FEES_BPS,
    impact_coeff: float = 0.6,
) -> dict[str, float]:
    """Almgren-Chriss-style square-root market impact, in bps.

    cost_bps = fixed_fees + impact_coeff × daily_vol_pct × 100 × sqrt(participation)

    where `participation = trade_value / avg_daily_value`.

    Calibration: `impact_coeff` ~0.5-0.6 fits empirical NSE intraday slippage
    (cf. Frino et al. 2015 on Asia-Pacific markets).
    """
    if trade_value_inr <= 0 or avg_daily_value_inr <= 0:
        return {"total_bps": fixed_fees_bps, "impact_bps": 0.0, "fixed_bps": fixed_fees_bps}
    participation = trade_value_inr / avg_daily_value_inr
    impact_bps = impact_coeff * daily_vol_pct * 100 * math.sqrt(max(0.0, participation))
    return {
        "total_bps": round(fixed_fees_bps + impact_bps, 2),
        "impact_bps": round(impact_bps, 2),
        "fixed_bps": round(fixed_fees_bps, 2),
        "participation_pct": round(participation * 100, 3),
    }


def round_trip_cost_pct(
    *,
    trade_value_inr: float,
    avg_daily_value_inr: float,
    daily_vol_pct: float,
) -> float:
    """Round-trip (enter + exit) cost as a percentage of trade value.

    Returns 0.20 for a tiny trade on a hyper-liquid stock; up to ~1.0+
    for a 5% ADV order on a thin name. Use this where the backtester
    previously hard-coded 0.20.
    """
    one_way = sqrt_impact_cost_bps(
        trade_value_inr=trade_value_inr,
        avg_daily_value_inr=avg_daily_value_inr,
        daily_vol_pct=daily_vol_pct,
    )
    return round(one_way["total_bps"] * 2 / 100.0, 4)


# ── PIT fundamentals snapshots ──────────────────────────────────────────

async def _ensure_pit_table() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS fundamentals_pit (
                symbol TEXT NOT NULL,
                as_of_date TEXT NOT NULL,
                source TEXT NOT NULL,
                fundamentals_json TEXT NOT NULL,
                composite_score INTEGER,
                created_at TEXT NOT NULL,
                PRIMARY KEY (symbol, as_of_date)
            )"""
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fundpit_symbol_date ON fundamentals_pit(symbol, as_of_date)"
        )
        await db.commit()


async def snapshot_fundamentals(
    symbol: str,
    fundamentals: dict[str, Any],
    *,
    source: str = "yfinance",
    composite_score: Optional[int] = None,
) -> None:
    """Persist today's fundamentals snapshot for `symbol`.

    The backtester should call `load_fundamentals_as_of(symbol, t)`
    instead of fetching fresh yfinance numbers — yfinance returns
    *current restated* financials, which leak future-knowledge into
    historical bars.
    """
    await _ensure_pit_table()
    import json
    as_of = datetime.now(timezone.utc).date().isoformat()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT OR REPLACE INTO fundamentals_pit
                     (symbol, as_of_date, source, fundamentals_json,
                      composite_score, created_at)
                     VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    symbol, as_of, source, json.dumps(fundamentals, default=str),
                    int(composite_score) if composite_score is not None else None,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            await db.commit()
    except Exception as e:
        logger.debug("snapshot_fundamentals skipped for %s: %s", symbol, e)


async def load_fundamentals_as_of(symbol: str, as_of: str) -> Optional[dict[str, Any]]:
    """Return the most recent snapshot for `symbol` that is ≤ `as_of` (ISO date).

    Use this in backtests to avoid look-ahead bias. Returns None when
    no historical snapshot exists for the symbol on or before that date.
    """
    await _ensure_pit_table()
    import json
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT fundamentals_json, as_of_date, composite_score
                   FROM fundamentals_pit
                   WHERE symbol = ? AND as_of_date <= ?
                   ORDER BY as_of_date DESC LIMIT 1""",
                (symbol, as_of),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        data = json.loads(row["fundamentals_json"])
        data["_pit_as_of_date"] = row["as_of_date"]
        data["_pit_composite_score"] = row["composite_score"]
        return data
    except Exception as e:
        logger.debug("load_fundamentals_as_of failed for %s: %s", symbol, e)
        return None


# ─────────────────────────────────────────────────────────────────────────
# apply_costs — drop-in for backtest + live tracker P&L.
# Applies brokerage + STT + slippage + DP charges in one call so the
# downstream system sees realistic net returns.
# ─────────────────────────────────────────────────────────────────────────


def _slippage_bps(*, avg_daily_volume: Optional[float], volatile: bool = False) -> float:
    """Per-side slippage in bps. Falls back to a mid-cap 10bp default when
    avg_daily_volume is None.
    """
    if avg_daily_volume is None or avg_daily_volume <= 0:
        return 15.0 if volatile else 10.0
    # Liquid (≥ 1M shares/day) → 5bp; mid (≥ 100k) → 10bp; thin → 15bp.
    if avg_daily_volume >= 1_000_000:
        return 7.0 if volatile else 5.0
    if avg_daily_volume >= 100_000:
        return 12.0 if volatile else 10.0
    return 18.0 if volatile else 15.0


def apply_costs(
    *,
    entry: float,
    exit: float,
    qty: int,
    segment: str = "cash",          # "cash" | "fno" | "intraday"
    holding_days: int = 1,
    avg_daily_volume: Optional[float] = None,
    volatile: bool = False,
) -> dict[str, float]:
    """Net P&L on a single position after brokerage, STT, DP, slippage, GST.

    Returns: {gross_pnl, net_pnl, total_costs_inr, breakdown{...}}.

    Indian brokerage assumptions match Zerodha-tier discount broker pricing:
      • Equity delivery: 0% brokerage, STT 0.1% each side, exchange fee
        0.00345%, GST 18% on (brokerage + exchange + SEBI), DP ₹13.5/scrip/sell.
      • Equity intraday: brokerage min(20, 0.03% per side), STT 0.025%
        on sell side only.
      • F&O: brokerage min(20, 0.03%), STT 0.0625% on sell premium for
        options / 0.0125% on sell for futures.
    """
    if qty <= 0 or entry <= 0 or exit <= 0:
        return {"gross_pnl": 0.0, "net_pnl": 0.0, "total_costs_inr": 0.0, "breakdown": {}}

    notional_buy = entry * qty
    notional_sell = exit * qty

    breakdown: dict[str, float] = {}

    if segment == "cash":
        # Delivery — zero brokerage on most discount platforms.
        brokerage = 0.0
        stt = 0.001 * notional_buy + 0.001 * notional_sell
        exch = 0.0000345 * (notional_buy + notional_sell)
        sebi = 0.000001 * (notional_buy + notional_sell)  # ₹10/Cr
        dp = 13.5 if qty > 0 else 0.0
        gst = 0.18 * (brokerage + exch + sebi)
        breakdown.update({
            "brokerage": brokerage, "stt": stt, "exchange": exch,
            "sebi": sebi, "dp": dp, "gst": gst,
        })
    elif segment == "intraday":
        brokerage = min(20.0, 0.0003 * notional_buy) + min(20.0, 0.0003 * notional_sell)
        stt = 0.00025 * notional_sell
        exch = 0.0000345 * (notional_buy + notional_sell)
        sebi = 0.000001 * (notional_buy + notional_sell)
        gst = 0.18 * (brokerage + exch + sebi)
        dp = 0.0
        breakdown.update({
            "brokerage": brokerage, "stt": stt, "exchange": exch,
            "sebi": sebi, "dp": dp, "gst": gst,
        })
    else:  # fno
        brokerage = min(20.0, 0.0003 * notional_buy) + min(20.0, 0.0003 * notional_sell)
        stt = 0.000625 * notional_sell  # options-side default
        exch = 0.00053 * (notional_buy + notional_sell)
        sebi = 0.000001 * (notional_buy + notional_sell)
        gst = 0.18 * (brokerage + exch + sebi)
        dp = 0.0
        breakdown.update({
            "brokerage": brokerage, "stt": stt, "exchange": exch,
            "sebi": sebi, "dp": dp, "gst": gst,
        })

    # Slippage — symmetric per-side bps applied to notional.
    slip_bps = _slippage_bps(avg_daily_volume=avg_daily_volume, volatile=volatile)
    slippage = (slip_bps / 10_000.0) * (notional_buy + notional_sell)
    breakdown["slippage"] = round(slippage, 2)

    total_costs = sum(breakdown.values())
    gross_pnl = (exit - entry) * qty
    net_pnl = gross_pnl - total_costs

    return {
        "gross_pnl": round(gross_pnl, 2),
        "net_pnl": round(net_pnl, 2),
        "total_costs_inr": round(total_costs, 2),
        "breakdown": {k: round(v, 2) for k, v in breakdown.items()},
    }


def simulate_slippage_fill(
    *,
    bar_open: float,
    direction: str,                  # "bullish" | "bearish"
    avg_daily_volume: Optional[float] = None,
    volatile: bool = False,
) -> float:
    """Adverse-side slippage fill on entry — used by the backtester so it
    can't pretend to fill at the exact bar_open. Pulls bps from the same
    table apply_costs uses, then nudges the fill price the wrong way.
    """
    bps = _slippage_bps(avg_daily_volume=avg_daily_volume, volatile=volatile)
    drag = bar_open * (bps / 10_000.0)
    return round(bar_open + drag if direction == "bullish" else bar_open - drag, 2)


def monte_carlo_signal_order(
    pnls: list[float],
    *,
    iterations: int = 1000,
    seed: Optional[int] = 42,
) -> dict[str, float]:
    """Resample the *order* of trade outcomes to compute a distribution of
    Sharpe / WR / cumulative-PnL.

    Why: the standard chronological backtest gives one number — but if the
    win sequence was lucky (a few big wins front-loaded), the strategy
    might be fragile. Monte-Carlo over signal-order tests whether the
    cumulative path is robust.
    """
    import random
    import statistics

    if not pnls or iterations <= 0:
        return {"iterations": 0, "wr_p5": 0.0, "wr_p50": 0.0, "wr_p95": 0.0,
                "sharpe_p5": 0.0, "sharpe_p50": 0.0, "sharpe_p95": 0.0}

    rng = random.Random(seed)
    wrs: list[float] = []
    sharpes: list[float] = []
    arr = list(pnls)
    n = len(arr)

    for _ in range(iterations):
        rng.shuffle(arr)
        wins = sum(1 for x in arr if x > 0)
        wrs.append(wins / n * 100.0)
        if n > 1:
            mu = statistics.mean(arr)
            sd = statistics.stdev(arr)
            sharpes.append((mu / sd) if sd > 0 else 0.0)
        else:
            sharpes.append(0.0)

    def pct(xs: list[float], p: float) -> float:
        if not xs:
            return 0.0
        s = sorted(xs)
        idx = max(0, min(len(s) - 1, int(p * (len(s) - 1))))
        return round(s[idx], 3)

    return {
        "iterations": iterations,
        "n_trades": n,
        "wr_p5": pct(wrs, 0.05),
        "wr_p50": pct(wrs, 0.50),
        "wr_p95": pct(wrs, 0.95),
        "sharpe_p5": pct(sharpes, 0.05),
        "sharpe_p50": pct(sharpes, 0.50),
        "sharpe_p95": pct(sharpes, 0.95),
    }

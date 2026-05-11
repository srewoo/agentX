"""Portfolio analytics service.

Pure-ish business logic for the /api/portfolio/* endpoints. Keeps SQL,
math, and external I/O in clearly separated helpers so each piece is
unit-testable without spinning up FastAPI.

Computes:
    * Open positions (FIFO lots) with live mark-to-market
    * Realized + unrealized P&L (FIFO)
    * Day P&L vs prev close
    * Sharpe ratio (annualized, daily, configurable rf)
    * Max drawdown on equity curve
    * Beta vs Nifty 50 (252-day OLS regression)
    * Sector exposure (% capital, with concentration flags)
    * Win rate, avg win/loss, profit factor

Design notes:
    * FIFO is the source of truth for realized P&L. We never trust a
      "pnl" column on a transaction row — we always recompute from the
      ledger so corrections are easy and audits are deterministic.
    * Live LTP failures fall back to last transaction price. Logged at
      WARNING — silent fallback would hide a broken market-data path.
    * All money values returned to callers are rounded to 2 dp INR.
      Internal math uses float; switch to Decimal here if precision
      complaints surface (none yet).
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import aiosqlite

from app.database import DB_PATH
from app.services.data_fetcher import MAJOR_STOCKS

logger = logging.getLogger(__name__)


# ── Tunables (named constants, never magic numbers) ───────────
DEFAULT_RISK_FREE_RATE: float = 0.07   # INR risk-free; configurable per call
TRADING_DAYS_PER_YEAR: int = 252
BETA_WINDOW_DAYS: int = 252
CONCENTRATION_FLAG_PCT: float = 20.0   # single-position warning
SECTOR_CONCENTRATION_FLAG_PCT: float = 35.0
DEFAULT_PAGE_LIMIT: int = 100
MAX_PAGE_LIMIT: int = 500
PORTFOLIO_POSITION_WARN_PCT: float = 15.0
PORTFOLIO_POSITION_BLOCK_PCT: float = 25.0
PORTFOLIO_SECTOR_WARN_PCT: float = 25.0
PORTFOLIO_SECTOR_BLOCK_PCT: float = 40.0

_MIGRATION_PATH: str = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "database_migrations",
    "portfolio_tables.sql",
)


# ── Domain types ──────────────────────────────────────────────
@dataclass
class Lot:
    """A single FIFO buy lot waiting to be matched against sells."""
    qty: float
    price: float
    ts: str


@dataclass
class RealizedTrade:
    """One realized round-trip (matched buy→sell slice)."""
    symbol: str
    qty: float
    buy_price: float
    sell_price: float
    buy_ts: str
    sell_ts: str
    pnl: float


@dataclass
class FIFOResult:
    realized: list[RealizedTrade] = field(default_factory=list)
    open_lots: dict[str, list[Lot]] = field(default_factory=dict)


# ── Schema bootstrap ──────────────────────────────────────────
async def ensure_schema() -> None:
    """Apply the portfolio_tables.sql migration. Idempotent."""
    with open(_MIGRATION_PATH, "r", encoding="utf-8") as f:
        sql = f.read()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(sql)
        await db.commit()


# ── Transaction ledger ────────────────────────────────────────
async def insert_transaction(
    *,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    fees: float = 0.0,
    notes: Optional[str] = None,
    ts: Optional[str] = None,
) -> dict[str, Any]:
    """Append a single fill to the ledger.

    Args:
        symbol: NSE/BSE ticker (already sanitized by caller).
        side: 'BUY' or 'SELL'.
        qty: Positive share count.
        price: Per-share fill price in INR.
        fees: Brokerage + STT + other charges.
        notes: Free-form note (max 500 chars enforced at the router).
        ts: ISO-8601 UTC; defaults to now.

    Returns:
        The inserted row as a dict.

    Raises:
        ValueError: On invalid side / non-positive qty / negative price.
    """
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise ValueError(f"side must be BUY or SELL, got {side!r}")
    if qty <= 0:
        raise ValueError("qty must be positive")
    if price < 0:
        raise ValueError("price must be non-negative")

    row = {
        "id": uuid.uuid4().hex,
        "ts": ts or datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "side": side,
        "qty": float(qty),
        "price": float(price),
        "fees": float(fees),
        "notes": notes,
    }
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO transactions (id, ts, symbol, side, qty, price, fees, notes)
            VALUES (:id, :ts, :symbol, :side, :qty, :price, :fees, :notes)
            """,
            row,
        )
        await db.commit()
    logger.info("portfolio.tx inserted symbol=%s side=%s qty=%.4f price=%.2f", symbol, side, qty, price)
    return row


async def list_transactions(
    *,
    symbol: Optional[str] = None,
    from_ts: Optional[str] = None,
    to_ts: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: int = DEFAULT_PAGE_LIMIT,
) -> dict[str, Any]:
    """Cursor-paginated transaction list, newest first.

    Cursor is the ts of the last row of the previous page.
    """
    limit = max(1, min(limit, MAX_PAGE_LIMIT))
    where: list[str] = []
    params: list[Any] = []
    if symbol:
        where.append("symbol = ?")
        params.append(symbol)
    if from_ts:
        where.append("ts >= ?")
        params.append(from_ts)
    if to_ts:
        where.append("ts <= ?")
        params.append(to_ts)
    if cursor:
        where.append("ts < ?")
        params.append(cursor)
    sql = "SELECT * FROM transactions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit + 1)  # fetch one extra to know if there's a next page

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    next_cursor: Optional[str] = None
    if len(rows) > limit:
        next_cursor = rows[limit - 1]["ts"]
        rows = rows[:limit]
    return {"transactions": rows, "next_cursor": next_cursor}


async def fetch_all_transactions_chronological() -> list[dict[str, Any]]:
    """Return every transaction in (ts ASC) order — input for FIFO."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM transactions ORDER BY ts ASC") as cur:
            return [dict(r) for r in await cur.fetchall()]


# ── FIFO P&L ──────────────────────────────────────────────────
def compute_fifo(transactions: Iterable[dict[str, Any]]) -> FIFOResult:
    """Walk a chronological transaction ledger and build FIFO realized + open lots.

    Pure function — no I/O. Easy to golden-test.

    Args:
        transactions: ts-ascending iterable of ledger rows.

    Returns:
        FIFOResult containing every realized round-trip and the remaining
        open lots per symbol.

    Raises:
        ValueError: On a SELL larger than the available open lots — this
            is a data integrity bug (short sales aren't supported here)
            and we want a loud failure rather than negative inventory.
    """
    open_lots: dict[str, deque[Lot]] = defaultdict(deque)
    realized: list[RealizedTrade] = []

    for tx in transactions:
        symbol = tx["symbol"]
        side = tx["side"]
        qty = float(tx["qty"])
        price = float(tx["price"])
        fees = float(tx.get("fees") or 0.0)
        ts = tx["ts"]

        if side == "BUY":
            # Fees on the buy raise the effective cost basis.
            effective_price = price + (fees / qty if qty else 0.0)
            open_lots[symbol].append(Lot(qty=qty, price=effective_price, ts=ts))
            continue

        # SELL — match against oldest lots first.
        remaining = qty
        # Per-share fee for this sell — applied across matched slices proportionally.
        sell_fee_per_share = fees / qty if qty else 0.0
        lots = open_lots[symbol]
        while remaining > 0:
            if not lots:
                raise ValueError(
                    f"SELL of {qty} {symbol} at {ts} exceeds available lots — "
                    "ledger is inconsistent (short sales unsupported)"
                )
            lot = lots[0]
            matched_qty = min(lot.qty, remaining)
            net_sell = price - sell_fee_per_share
            pnl = (net_sell - lot.price) * matched_qty
            realized.append(
                RealizedTrade(
                    symbol=symbol,
                    qty=matched_qty,
                    buy_price=lot.price,
                    sell_price=net_sell,
                    buy_ts=lot.ts,
                    sell_ts=ts,
                    pnl=pnl,
                )
            )
            lot.qty -= matched_qty
            remaining -= matched_qty
            if lot.qty <= 1e-9:
                lots.popleft()

    return FIFOResult(
        realized=realized,
        open_lots={k: list(v) for k, v in open_lots.items() if v},
    )


# ── Equity curve & risk metrics ───────────────────────────────
def realized_equity_curve(realized: list[RealizedTrade]) -> list[tuple[str, float]]:
    """Cumulative realized P&L by sell timestamp. (date, equity) tuples."""
    curve: list[tuple[str, float]] = []
    running = 0.0
    for rt in sorted(realized, key=lambda r: r.sell_ts):
        running += rt.pnl
        curve.append((rt.sell_ts, running))
    return curve


def max_drawdown(equity_curve: list[float]) -> float:
    """Peak-to-trough drawdown as a positive number (worst loss from a peak).

    Returns 0 if the curve has fewer than 2 points or is monotonically rising.
    """
    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    worst = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > worst:
            worst = dd
    return worst


def sharpe_ratio(
    daily_returns: list[float],
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> float:
    """Annualized Sharpe from a daily return series.

    Args:
        daily_returns: Day-over-day fractional returns (e.g. 0.012 = +1.2%).
        risk_free_rate: Annualized risk-free rate (e.g. 0.07 for 7%).

    Returns:
        Annualized Sharpe. 0.0 if stdev is 0 or sample is too small —
        we don't raise because callers want a number for the dashboard,
        not an exception. They can detect "no data" via N elsewhere.
    """
    n = len(daily_returns)
    if n < 2:
        return 0.0
    daily_rf = risk_free_rate / TRADING_DAYS_PER_YEAR
    excess = [r - daily_rf for r in daily_returns]
    mean = sum(excess) / n
    var = sum((x - mean) ** 2 for x in excess) / (n - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (mean / std) * math.sqrt(TRADING_DAYS_PER_YEAR)


def beta(
    portfolio_returns: list[float],
    benchmark_returns: list[float],
) -> float:
    """OLS beta of portfolio vs benchmark (cov / var).

    Both series must align day-for-day. Returns 0.0 if benchmark has no
    variance or the series are too short — same rationale as Sharpe.
    """
    n = min(len(portfolio_returns), len(benchmark_returns))
    if n < 2:
        return 0.0
    p = portfolio_returns[-n:]
    b = benchmark_returns[-n:]
    mean_p = sum(p) / n
    mean_b = sum(b) / n
    cov = sum((p[i] - mean_p) * (b[i] - mean_b) for i in range(n)) / (n - 1)
    var_b = sum((b[i] - mean_b) ** 2 for i in range(n)) / (n - 1)
    if var_b == 0:
        return 0.0
    return cov / var_b


def win_metrics(realized: list[RealizedTrade]) -> dict[str, float]:
    """Win rate, avg win, avg loss, profit factor, expectancy."""
    if not realized:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "profit_factor": 0.0,
            "expectancy": 0.0,
        }
    wins = [r.pnl for r in realized if r.pnl > 0]
    losses = [r.pnl for r in realized if r.pnl < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0
    win_rate = len(wins) / len(realized)
    # profit_factor: inf is intentional when there are no losses; cap for JSON.
    if gross_loss == 0:
        profit_factor = float("inf") if gross_win > 0 else 0.0
    else:
        profit_factor = gross_win / gross_loss
    expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
    return {
        "trades": len(realized),
        "win_rate": round(win_rate, 4),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": (
            round(profit_factor, 4) if math.isfinite(profit_factor) else None  # type: ignore[arg-type]
        ),
        "expectancy": round(expectancy, 2),
    }


# ── Live mark-to-market ───────────────────────────────────────
async def _live_quote(symbol: str) -> dict[str, Any]:
    """Best-effort LTP fetch. Never raises — callers degrade gracefully."""
    try:
        from app.services.data_fetcher import get_stock_quote
        return await get_stock_quote(symbol)
    except Exception as exc:  # noqa: BLE001 — boundary, must not blow up portfolio render
        logger.warning("portfolio.live_quote failed symbol=%s err=%s", symbol, exc)
        return {"symbol": symbol, "lastPrice": None}


async def open_positions_with_marks(
    open_lots: dict[str, list[Lot]],
) -> list[dict[str, Any]]:
    """Aggregate FIFO open lots per symbol and mark-to-market in parallel.

    Returns one row per symbol with:
        symbol, qty, avg_price, ltp, prev_close, market_value,
        unrealized_pnl, day_pnl, day_pnl_pct.
    """
    if not open_lots:
        return []

    symbols = list(open_lots.keys())
    quotes = await asyncio.gather(*(_live_quote(s) for s in symbols))

    rows: list[dict[str, Any]] = []
    for symbol, quote in zip(symbols, quotes):
        lots = open_lots[symbol]
        total_qty = sum(lot.qty for lot in lots)
        if total_qty <= 0:
            continue
        cost_basis = sum(lot.qty * lot.price for lot in lots)
        avg_price = cost_basis / total_qty

        ltp = quote.get("lastPrice")
        prev_close = quote.get("previousClose")
        # Fallback so the dashboard isn't blank when the data fetcher trips.
        effective_ltp = ltp if ltp is not None else avg_price

        market_value = effective_ltp * total_qty
        unrealized = market_value - cost_basis
        day_pnl: Optional[float] = None
        day_pnl_pct: Optional[float] = None
        if ltp is not None and prev_close:
            day_pnl = (ltp - prev_close) * total_qty
            day_pnl_pct = ((ltp / prev_close) - 1.0) * 100.0

        rows.append({
            "symbol": symbol,
            "qty": round(total_qty, 4),
            "avg_price": round(avg_price, 2),
            "ltp": round(ltp, 2) if ltp is not None else None,
            "prev_close": round(prev_close, 2) if prev_close else None,
            "market_value": round(market_value, 2),
            "cost_basis": round(cost_basis, 2),
            "unrealized_pnl": round(unrealized, 2),
            "day_pnl": round(day_pnl, 2) if day_pnl is not None else None,
            "day_pnl_pct": round(day_pnl_pct, 2) if day_pnl_pct is not None else None,
            "ltp_stale": ltp is None,
        })
    return rows


# ── Sector exposure ───────────────────────────────────────────
async def _sector_for(symbol: str) -> str:
    """Best-effort sector lookup via data_fetcher.get_stock_info_async."""
    try:
        from app.services.data_fetcher import get_stock_info_async
        info = await get_stock_info_async(symbol)
        return (info or {}).get("sector") or "Unknown"
    except Exception as exc:  # noqa: BLE001
        logger.debug("portfolio.sector lookup failed symbol=%s err=%s", symbol, exc)
        return "Unknown"


async def sector_exposure(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate market value by sector and compute % weights + flags."""
    if not positions:
        return []
    total_mv = sum(p["market_value"] for p in positions) or 1.0
    sectors = await asyncio.gather(*(_sector_for(p["symbol"]) for p in positions))

    bucket: dict[str, dict[str, float]] = defaultdict(lambda: {"mv": 0.0, "day_pnl": 0.0})
    for pos, sector in zip(positions, sectors):
        bucket[sector]["mv"] += pos["market_value"]
        if pos.get("day_pnl") is not None:
            bucket[sector]["day_pnl"] += pos["day_pnl"]

    out: list[dict[str, Any]] = []
    for sector, agg in bucket.items():
        weight = (agg["mv"] / total_mv) * 100.0
        out.append({
            "sector": sector,
            "market_value": round(agg["mv"], 2),
            "weight_pct": round(weight, 2),
            "day_pnl": round(agg["day_pnl"], 2),
            "concentration_flag": weight > SECTOR_CONCENTRATION_FLAG_PCT,
        })
    out.sort(key=lambda r: r["weight_pct"], reverse=True)
    return out


# ── Summary ───────────────────────────────────────────────────
async def build_summary(risk_free_rate: float = DEFAULT_RISK_FREE_RATE) -> dict[str, Any]:
    """Headline metrics for /api/portfolio/summary.

    One pass over the ledger; one fan-out for live quotes; one sector pass.
    """
    txs = await fetch_all_transactions_chronological()
    fifo = compute_fifo(txs)

    positions = await open_positions_with_marks(fifo.open_lots)

    realized_pnl = sum(r.pnl for r in fifo.realized)
    unrealized_pnl = sum(p["unrealized_pnl"] for p in positions)
    day_pnl = sum((p["day_pnl"] or 0.0) for p in positions)
    market_value = sum(p["market_value"] for p in positions)

    # Daily return series from realized equity curve.
    realized_curve = realized_equity_curve(fifo.realized)
    daily_returns = _curve_to_daily_returns(realized_curve)
    sharpe = sharpe_ratio(daily_returns, risk_free_rate=risk_free_rate)
    dd = max_drawdown([eq for _, eq in realized_curve])

    # Concentration flags.
    flags: list[str] = []
    if market_value > 0:
        for p in positions:
            weight = (p["market_value"] / market_value) * 100.0
            if weight > CONCENTRATION_FLAG_PCT:
                flags.append(f"{p['symbol']} is {weight:.1f}% of portfolio")

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "market_value": round(market_value, 2),
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "total_pnl": round(realized_pnl + unrealized_pnl, 2),
        "day_pnl": round(day_pnl, 2),
        "sharpe": round(sharpe, 4),
        "max_drawdown": round(dd, 2),
        "beta_nifty": None,  # filled by caller if benchmark series available
        "win_metrics": win_metrics(fifo.realized),
        "open_positions": len(positions),
        "concentration_flags": flags,
        "risk_free_rate": risk_free_rate,
    }


def _static_sector_for(symbol: str) -> str:
    for item in MAJOR_STOCKS:
        if item.get("symbol") == symbol:
            return item.get("sector") or "Unknown"
    return "Unknown"


async def portfolio_recommendation_context(
    *,
    symbol: str,
    sector: str,
    action: str,
) -> dict[str, Any]:
    """Portfolio-aware adjustment for a fresh recommendation.

    Uses the transaction ledger only, not live quotes, so recommendation
    generation does not depend on extra market-data calls. Values are based on
    open-lot cost basis, which is stable enough for concentration gating.
    """
    try:
        await ensure_schema()
        txs = await fetch_all_transactions_chronological()
        fifo = compute_fifo(txs)
    except Exception as exc:
        logger.debug("portfolio recommendation context unavailable: %s", exc)
        return {
            "available": False,
            "action_adjustment": 0,
            "notes": ["Portfolio context unavailable."],
        }

    open_lots = fifo.open_lots
    position_cost: dict[str, float] = {
        sym: sum(lot.qty * lot.price for lot in lots)
        for sym, lots in open_lots.items()
    }
    total_cost = sum(position_cost.values())
    symbol_cost = position_cost.get(symbol, 0.0)
    symbol_weight = (symbol_cost / total_cost * 100.0) if total_cost > 0 else 0.0

    sector_cost = 0.0
    for sym, cost in position_cost.items():
        if _static_sector_for(sym) == sector:
            sector_cost += cost
    sector_weight = (sector_cost / total_cost * 100.0) if total_cost > 0 else 0.0

    notes: list[str] = []
    adjustment = 0
    decision = "neutral"

    if action == "BUY":
        if symbol_weight >= PORTFOLIO_POSITION_BLOCK_PCT:
            adjustment -= 25
            decision = "block_add"
            notes.append(f"Already {symbol_weight:.1f}% allocated to {symbol}.")
        elif symbol_weight >= PORTFOLIO_POSITION_WARN_PCT:
            adjustment -= 10
            decision = "reduce_size"
            notes.append(f"{symbol} is already {symbol_weight:.1f}% of portfolio.")

        if sector_weight >= PORTFOLIO_SECTOR_BLOCK_PCT:
            adjustment -= 20
            decision = "block_add"
            notes.append(f"{sector} exposure is already {sector_weight:.1f}%.")
        elif sector_weight >= PORTFOLIO_SECTOR_WARN_PCT:
            adjustment -= 8
            if decision == "neutral":
                decision = "reduce_size"
            notes.append(f"{sector} exposure is elevated at {sector_weight:.1f}%.")

    elif action == "SELL":
        if symbol_cost > 0:
            adjustment += 8
            decision = "existing_position_exit"
            notes.append(f"Existing {symbol} position found; sell signal is portfolio-relevant.")
        else:
            notes.append("No existing position found; treat SELL as watchlist/short-bias signal.")

    return {
        "available": True,
        "total_cost_basis": round(total_cost, 2),
        "symbol_cost_basis": round(symbol_cost, 2),
        "symbol_weight_pct": round(symbol_weight, 2),
        "sector_weight_pct": round(sector_weight, 2),
        "action_adjustment": adjustment,
        "decision": decision,
        "notes": notes,
    }


def _curve_to_daily_returns(curve: list[tuple[str, float]]) -> list[float]:
    """Bucket cumulative-equity curve to daily returns.

    Realized P&L is event-driven; we bucket by date so Sharpe sees a daily
    series rather than per-trade noise.
    """
    if len(curve) < 2:
        return []
    by_day: dict[str, float] = {}
    for ts, eq in curve:
        day = ts[:10]  # YYYY-MM-DD slice — ISO-8601 guarantees this works
        by_day[day] = eq  # last value of the day wins
    days = sorted(by_day.keys())
    eqs = [by_day[d] for d in days]
    returns: list[float] = []
    for i in range(1, len(eqs)):
        prev = eqs[i - 1]
        if prev == 0:
            # First day's "return" is undefined — skip rather than divide by 0.
            continue
        returns.append((eqs[i] - prev) / abs(prev))
    return returns


# ── Equity curve endpoint ─────────────────────────────────────
_PERIOD_TO_DAYS: dict[str, Optional[int]] = {
    "1m": 30,
    "3m": 90,
    "1y": 365,
    "all": None,
}


async def equity_curve(period: str = "all") -> list[dict[str, Any]]:
    """Combined realized + unrealized equity curve for charting.

    For now we return the realized cumulative curve (deterministic). MTM
    intra-trade reconstruction needs daily OHLC backfill — tracked as a
    follow-up.
    """
    if period not in _PERIOD_TO_DAYS:
        raise ValueError(f"period must be one of {list(_PERIOD_TO_DAYS)}")

    txs = await fetch_all_transactions_chronological()
    fifo = compute_fifo(txs)
    curve = realized_equity_curve(fifo.realized)

    cutoff_days = _PERIOD_TO_DAYS[period]
    if cutoff_days is not None and curve:
        cutoff = datetime.now(timezone.utc).timestamp() - cutoff_days * 86400
        curve = [
            (ts, eq) for ts, eq in curve
            if datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() >= cutoff
        ]
    return [{"ts": ts, "equity": round(eq, 2)} for ts, eq in curve]


# ── INR formatting helper ─────────────────────────────────────
def format_inr(amount: float) -> str:
    """Format INR amount with lakh/crore suffixes.

    Examples:
        12345.6   -> '₹12,345.60'
        125000    -> '₹1.25 L'
        12500000  -> '₹1.25 Cr'
    """
    sign = "-" if amount < 0 else ""
    a = abs(amount)
    if a >= 1_00_00_000:  # 1 crore
        return f"{sign}₹{a / 1_00_00_000:.2f} Cr"
    if a >= 1_00_000:     # 1 lakh
        return f"{sign}₹{a / 1_00_000:.2f} L"
    # Indian number formatting (1,23,456.78). f-string commas give Western
    # grouping, so we do the indian regrouping manually for readability.
    whole, frac = f"{a:.2f}".split(".")
    if len(whole) > 3:
        head = whole[:-3]
        tail = whole[-3:]
        # Group head in pairs from the right.
        grouped = ",".join(
            [head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1]
        )
        whole = f"{grouped},{tail}"
    return f"{sign}₹{whole}.{frac}"

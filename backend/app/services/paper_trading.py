from __future__ import annotations

"""Durable helpers for local paper-trade tracking.

The legacy paper-trade output is CSV-only. These helpers let the backend
import that file into SQLite and apply the same backtest edge gate before a
signal is allowed to become a paper trade.
"""

import csv
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from app.database import DB_PATH
from app.services.signal_edge import get_edge

PAPER_TRADE_FIELDS = [
    "trade_id",
    "symbol",
    "direction",
    "signal_type",
    "strength",
    "entry_price",
    "entry_date",
    "stop_loss",
    "target",
    "position_size",
    "shares",
    "status",
    "exit_price",
    "exit_date",
    "pnl_pct",
    "pnl_amount",
    "exit_reason",
    "trailing_stop",
]


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None


def _row_to_trade(row: aiosqlite.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return data
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


async def import_paper_trades_csv(csv_path: str | Path) -> dict[str, int]:
    """Import the local paper-trade CSV into SQLite with idempotent upserts."""
    path = Path(csv_path)
    if not path.exists():
        return {"imported": 0, "skipped": 0}

    imported = skipped = 0
    now = datetime.now(timezone.utc).isoformat()
    rows: list[tuple[Any, ...]] = []
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            trade_id = (raw.get("trade_id") or "").strip()
            if not trade_id:
                skipped += 1
                continue
            rows.append(
                (
                    trade_id,
                    raw.get("symbol"),
                    raw.get("direction"),
                    raw.get("signal_type"),
                    _int_or_none(raw.get("strength")) or 0,
                    _float_or_none(raw.get("entry_price")) or 0.0,
                    raw.get("entry_date"),
                    _float_or_none(raw.get("stop_loss")),
                    _float_or_none(raw.get("target")),
                    _float_or_none(raw.get("position_size")),
                    _int_or_none(raw.get("shares")),
                    raw.get("status") or "open",
                    _float_or_none(raw.get("exit_price")),
                    raw.get("exit_date") or None,
                    _float_or_none(raw.get("pnl_pct")),
                    _float_or_none(raw.get("pnl_amount")),
                    raw.get("exit_reason") or None,
                    _float_or_none(raw.get("trailing_stop")),
                    "csv_import",
                    now,
                    now,
                )
            )

    if rows:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executemany(
                """INSERT INTO paper_trades
                   (trade_id, symbol, direction, signal_type, strength,
                    entry_price, entry_date, stop_loss, target, position_size,
                    shares, status, exit_price, exit_date, pnl_pct, pnl_amount,
                    exit_reason, trailing_stop, source, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(trade_id) DO UPDATE SET
                    symbol=excluded.symbol,
                    direction=excluded.direction,
                    signal_type=excluded.signal_type,
                    strength=excluded.strength,
                    entry_price=excluded.entry_price,
                    entry_date=excluded.entry_date,
                    stop_loss=excluded.stop_loss,
                    target=excluded.target,
                    position_size=excluded.position_size,
                    shares=excluded.shares,
                    status=excluded.status,
                    exit_price=excluded.exit_price,
                    exit_date=excluded.exit_date,
                    pnl_pct=excluded.pnl_pct,
                    pnl_amount=excluded.pnl_amount,
                    exit_reason=excluded.exit_reason,
                    trailing_stop=excluded.trailing_stop,
                    updated_at=excluded.updated_at""",
                rows,
            )
            await db.commit()
        imported = len(rows)
    return {"imported": imported, "skipped": skipped}


async def create_paper_trade(
    *,
    symbol: str,
    direction: str,
    signal_type: str,
    strength: int,
    entry_price: float,
    entry_date: str | None = None,
    stop_loss: float | None = None,
    target: float | None = None,
    position_size: float | None = None,
    shares: int | None = None,
    trailing_stop: float | None = None,
    source: str = "api",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    trade = {
        "trade_id": uuid.uuid4().hex[:12],
        "symbol": symbol,
        "direction": direction,
        "signal_type": signal_type,
        "strength": max(1, min(10, int(strength))),
        "entry_price": float(entry_price),
        "entry_date": entry_date or now[:10],
        "stop_loss": stop_loss,
        "target": target,
        "position_size": position_size,
        "shares": shares,
        "status": "open",
        "exit_price": None,
        "exit_date": None,
        "pnl_pct": None,
        "pnl_amount": None,
        "exit_reason": None,
        "trailing_stop": trailing_stop,
        "source": source,
        "created_at": now,
        "updated_at": now,
    }
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO paper_trades
               (trade_id, symbol, direction, signal_type, strength, entry_price,
                entry_date, stop_loss, target, position_size, shares, status,
                exit_price, exit_date, pnl_pct, pnl_amount, exit_reason,
                trailing_stop, source, created_at, updated_at)
               VALUES (:trade_id, :symbol, :direction, :signal_type, :strength,
                :entry_price, :entry_date, :stop_loss, :target, :position_size,
                :shares, :status, :exit_price, :exit_date, :pnl_pct, :pnl_amount,
                :exit_reason, :trailing_stop, :source, :created_at, :updated_at)""",
            trade,
        )
        await db.commit()
    return trade


async def close_paper_trade(
    trade_id: str,
    *,
    exit_price: float,
    exit_date: str | None = None,
    exit_reason: str = "manual",
) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM paper_trades WHERE trade_id = ?", (trade_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return None

        trade = dict(row)
        entry = float(trade["entry_price"])
        pnl_pct = ((float(exit_price) - entry) / entry) * 100.0 if entry else 0.0
        if trade["direction"] == "bearish":
            pnl_pct = -pnl_pct
        shares = trade.get("shares") or 0
        pnl_amount = (float(exit_price) - entry) * shares
        if trade["direction"] == "bearish":
            pnl_amount = -pnl_amount

        await db.execute(
            """UPDATE paper_trades
               SET status='closed', exit_price=?, exit_date=?, pnl_pct=?,
                   pnl_amount=?, exit_reason=?, updated_at=?
               WHERE trade_id=?""",
            (
                float(exit_price),
                exit_date or now[:10],
                round(pnl_pct, 4),
                round(pnl_amount, 2),
                exit_reason,
                now,
                trade_id,
            ),
        )
        await db.commit()

        async with db.execute("SELECT * FROM paper_trades WHERE trade_id = ?", (trade_id,)) as cur:
            updated = await cur.fetchone()
    return _row_to_trade(updated) if updated else None


async def list_paper_trades(
    *,
    status: str | None = None,
    symbol: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("status = ?")
        params.append(status)
    if symbol:
        where.append("symbol = ?")
        params.append(symbol)
    sql = "SELECT * FROM paper_trades"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY entry_date DESC, updated_at DESC LIMIT ?"
    params.append(limit)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            rows = [_row_to_trade(r) for r in await cur.fetchall()]
    return {"trades": rows, "count": len(rows)}


async def paper_trade_summary() -> dict[str, Any]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT status, COUNT(*) AS count,
                      SUM(COALESCE(pnl_amount, 0)) AS pnl_amount,
                      AVG(pnl_pct) AS avg_pnl_pct
               FROM paper_trades GROUP BY status"""
        ) as cur:
            by_status = [dict(r) for r in await cur.fetchall()]
        async with db.execute(
            """SELECT COUNT(*) AS closed,
                      SUM(CASE WHEN pnl_amount > 0 THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN pnl_amount < 0 THEN 1 ELSE 0 END) AS losses,
                      SUM(COALESCE(pnl_amount, 0)) AS total_pnl
               FROM paper_trades WHERE status='closed'"""
        ) as cur:
            totals = dict(await cur.fetchone())
    closed = totals.get("closed") or 0
    wins = totals.get("wins") or 0
    return {
        "by_status": by_status,
        "closed": closed,
        "wins": wins,
        "losses": totals.get("losses") or 0,
        "win_rate": round(wins / closed * 100.0, 2) if closed else 0.0,
        "total_pnl": round(totals.get("total_pnl") or 0.0, 2),
    }


def paper_trade_gate(signal: dict[str, Any], min_avg_pnl: float = 0.0) -> dict[str, Any]:
    """Return whether a signal is eligible for paper trading.

    Weak unconfirmed setups are rejected. Confirmed weak setups may pass, but
    only if their historical average P&L is not below `min_avg_pnl`.
    """
    signal_type = signal.get("signal_type", "")
    direction = signal.get("direction", "neutral")
    if direction == "neutral":
        return {"allowed": False, "reason": "neutral_signal"}

    metadata = signal.get("metadata") or {}
    if metadata.get("blocked_by_edge_filter"):
        return {"allowed": False, "reason": "blocked_by_edge_filter"}
    if metadata.get("downgraded_by_edge_filter") and not metadata.get("edge_confirmed"):
        return {"allowed": False, "reason": "weak_unconfirmed_edge"}

    edge = get_edge(signal_type, direction)
    if edge and edge.get("avg_pnl", 0.0) < min_avg_pnl and not metadata.get("edge_confirmed"):
        return {
            "allowed": False,
            "reason": "negative_historical_edge",
            "avg_pnl": edge.get("avg_pnl"),
            "win_rate": edge.get("win_rate"),
            "trades": edge.get("trades"),
        }
    return {"allowed": True, "reason": "passed_edge_gate"}

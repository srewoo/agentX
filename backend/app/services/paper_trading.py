from __future__ import annotations

"""Durable helpers for local paper-trade tracking.

The legacy paper-trade output is CSV-only. These helpers let the backend
import that file into SQLite and apply the same backtest edge gate before a
signal is allowed to become a paper trade.
"""

import csv
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

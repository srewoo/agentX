from __future__ import annotations

"""Price alert checker — evaluates user-defined price alerts against current market data."""

import logging
from datetime import datetime, timezone
from uuid import uuid4

import aiosqlite

from app.database import DB_PATH

logger = logging.getLogger(__name__)


async def create_alert(
    symbol: str,
    target_price: float,
    condition: str,
    current_price: float | None = None,
    note: str | None = None,
) -> dict:
    """Create a new price alert and return the created record."""
    alert_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO price_alerts
               (id, symbol, target_price, condition, current_price_at_creation,
                created_at, active, note)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
            (alert_id, symbol.upper(), target_price, condition, current_price, now, note),
        )
        await db.commit()

    logger.info(f"Created price alert {alert_id}: {symbol} {condition} {target_price}")
    return {
        "id": alert_id,
        "symbol": symbol.upper(),
        "target_price": target_price,
        "condition": condition,
        "current_price_at_creation": current_price,
        "created_at": now,
        "triggered_at": None,
        "triggered_price": None,
        "active": True,
        "note": note,
    }


async def get_active_alerts() -> list[dict]:
    """Return all active (untriggered) price alerts."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM price_alerts WHERE active = 1 ORDER BY created_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(row) for row in rows]


async def get_triggered_alerts() -> list[dict]:
    """Return all triggered (inactive) price alerts."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM price_alerts WHERE active = 0 ORDER BY triggered_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(row) for row in rows]


async def delete_alert(alert_id: str) -> bool:
    """Delete a price alert by ID. Returns True if a row was deleted."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM price_alerts WHERE id = ?", (alert_id,)
        )
        await db.commit()
        return cursor.rowcount > 0


async def check_alerts(prices: dict[str, float]) -> list[dict]:
    """
    Check all active alerts against current prices.

    For each alert whose condition is met, marks it as triggered, inserts a
    signal into the signals table, and returns the list of triggered signal dicts.
    """
    if not prices:
        return []

    active_alerts = await get_active_alerts()
    if not active_alerts:
        return []

    triggered_signals: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        for alert in active_alerts:
            symbol = alert["symbol"]
            current_price = prices.get(symbol)
            if current_price is None:
                continue

            target = alert["target_price"]
            condition = alert["condition"]
            triggered = False

            if condition == "above" and current_price >= target:
                triggered = True
            elif condition == "below" and current_price <= target:
                triggered = True
            elif condition == "pct_change":
                creation_price = alert.get("current_price_at_creation")
                pct_threshold = alert.get("pct_threshold") or target  # target_price doubles as threshold
                if creation_price and creation_price > 0:
                    change_pct = ((current_price - creation_price) / creation_price) * 100
                    if abs(change_pct) >= pct_threshold:
                        triggered = True

            if not triggered:
                continue

            # Mark alert as triggered
            await db.execute(
                """UPDATE price_alerts
                   SET active = 0, triggered_at = ?, triggered_price = ?
                   WHERE id = ?""",
                (now, current_price, alert["id"]),
            )

            # Build signal dict
            signal_id = str(uuid4())
            if condition == "pct_change":
                creation_price = alert.get("current_price_at_creation", 0) or 0
                change_pct = ((current_price - creation_price) / creation_price * 100) if creation_price > 0 else 0
                direction = "bullish" if change_pct > 0 else "bearish"
                reason = (
                    f"Price alert triggered: {symbol} moved {change_pct:+.1f}% "
                    f"(₹{creation_price:.2f} → ₹{current_price:.2f})"
                )
            else:
                direction = "bullish" if condition == "above" else "bearish"
                reason = (
                    f"Price alert triggered: {symbol} crossed {condition} "
                    f"\u20b9{target} (current: \u20b9{current_price})"
                )

            signal = {
                "id": signal_id,
                "symbol": symbol,
                "signal_type": "price_alert",
                "direction": direction,
                "strength": 8,
                "reason": reason,
                "risk": None,
                "llm_summary": None,
                "current_price": current_price,
                "metadata": {
                    "alert_id": alert["id"],
                    "target_price": target,
                    "condition": condition,
                    "note": alert.get("note"),
                },
                "created_at": now,
            }
            triggered_signals.append(signal)

            logger.info(
                f"Price alert triggered: {symbol} {condition} {target} "
                f"(current={current_price})"
            )

        await db.commit()

    return triggered_signals


def _row_to_dict(row: aiosqlite.Row) -> dict:
    """Convert a database Row to a plain dict with bool conversion for active."""
    return {
        "id": row["id"],
        "symbol": row["symbol"],
        "target_price": row["target_price"],
        "condition": row["condition"],
        "current_price_at_creation": row["current_price_at_creation"],
        "created_at": row["created_at"],
        "triggered_at": row["triggered_at"],
        "triggered_price": row["triggered_price"],
        "active": bool(row["active"]),
        "note": row["note"],
    }

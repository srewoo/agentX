from __future__ import annotations
"""Signal endpoints — latest, mark read, dismiss, manual trigger."""
import json
import logging
from typing import Optional

import aiosqlite
from fastapi import APIRouter, HTTPException

from app.database import DB_PATH
from app.services.orchestrator import run_scan_cycle

router = APIRouter(prefix="/api/signals", tags=["signals"])
logger = logging.getLogger(__name__)


def _row_to_signal(row: aiosqlite.Row) -> dict:
    d = dict(row)
    if isinstance(d.get("metadata"), str):
        try:
            d["metadata"] = json.loads(d["metadata"])
        except Exception:
            d["metadata"] = {}
    d["read"] = bool(d.get("read", 0))
    d["dismissed"] = bool(d.get("dismissed", 0))
    return d


@router.get("/latest")
async def get_latest_signals(since: Optional[str] = None, limit: int = 50):
    """Get latest signals, optionally filtered by timestamp."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if since:
            async with db.execute(
                """SELECT * FROM signals
                   WHERE dismissed = 0 AND created_at > ?
                   ORDER BY created_at DESC LIMIT ?""",
                (since, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with db.execute(
                """SELECT * FROM signals
                   WHERE dismissed = 0
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()

        signals = [_row_to_signal(r) for r in rows]

        async with db.execute(
            "SELECT COUNT(*) FROM signals WHERE read = 0 AND dismissed = 0"
        ) as cursor:
            count_row = await cursor.fetchone()
            unread_count = count_row[0] if count_row else 0

    return {"signals": signals, "unread_count": unread_count}


@router.post("/{signal_id}/read")
async def mark_signal_read(signal_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE signals SET read = 1 WHERE id = ?", (signal_id,))
        await db.commit()
    return {"ok": True}


@router.post("/{signal_id}/dismiss")
async def dismiss_signal(signal_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE signals SET dismissed = 1 WHERE id = ?", (signal_id,))
        await db.commit()
    return {"ok": True}


@router.post("/read-all")
async def mark_all_read():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE signals SET read = 1 WHERE dismissed = 0")
        await db.commit()
    return {"ok": True}

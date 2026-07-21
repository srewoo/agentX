from __future__ import annotations
"""Signal endpoints — latest, mark read, dismiss, manual trigger."""
import json
import logging
from typing import Optional

import aiosqlite
from fastapi import APIRouter, HTTPException

from app.database import DB_PATH
from app.services.orchestrator import run_scan_cycle
from app.services.thinking_analyst import analyze_signal_deep

# The Live feed shows only recent signals. Older undismissed rows persist in
# the DB for analytics but are NOT surfaced as if they were current — otherwise
# a quiet scan (0 new signals) leaves last week's stale cards on screen looking
# like fresh output. Callers can override via ?max_age_days=.
_FEED_MAX_AGE_DAYS = 7

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
async def get_latest_signals(
    since: Optional[str] = None,
    limit: int = 50,
    max_age_days: int = _FEED_MAX_AGE_DAYS,
):
    """Get latest signals, optionally filtered by timestamp.

    Signals older than ``max_age_days`` are never surfaced here, so a quiet
    scan shows an empty feed ("No new signals") instead of last week's stale
    cards dressed up as current output. The stronger of ``since`` and the
    age cutoff wins.
    """
    from datetime import datetime, timezone, timedelta
    age_cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    # Use the later (more recent) of an explicit `since` and the age cutoff.
    cutoff = max(since, age_cutoff) if since else age_cutoff
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # strength > 0 excludes signals that the edge filter has muted
        # (broken detectors like bullish_engulfing or the bullish leg of
        # rsi_extreme). They stay in the DB for analytics, but the Live
        # tab should not present a "SELL HOLD BUY" card the engine has
        # already deemed worthless.
        async with db.execute(
            """SELECT * FROM signals
               WHERE dismissed = 0 AND strength > 0 AND created_at > ?
               ORDER BY created_at DESC LIMIT ?""",
            (cutoff, limit),
        ) as cursor:
            rows = await cursor.fetchall()

        signals = [_row_to_signal(r) for r in rows]

        async with db.execute(
            "SELECT COUNT(*) FROM signals WHERE read = 0 AND dismissed = 0 "
            "AND strength > 0 AND created_at > ?",
            (age_cutoff,),
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


@router.post("/{signal_id}/deep-analysis")
async def deep_signal_analysis(signal_id: str, reasoning_effort: str = "medium"):
    """Run on-demand thinking-model review for one signal card."""
    if reasoning_effort not in {"low", "medium", "high"}:
        raise HTTPException(status_code=400, detail="reasoning_effort must be low, medium, or high")
    try:
        result = await analyze_signal_deep(signal_id, reasoning_effort=reasoning_effort)
        return {"data": result}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Deep signal analysis failed for %s: %s", signal_id, e)
        raise HTTPException(status_code=500, detail="Failed to run deep signal analysis")


@router.post("/read-all")
async def mark_all_read():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE signals SET read = 1 WHERE dismissed = 0")
        await db.commit()
    return {"ok": True}

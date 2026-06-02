from __future__ import annotations
"""Runtime heartbeats for the autonomous loops.

The scan / auto-paper / backtest loops are in-process asyncio tasks tied to
the server's lifetime — there's no external scheduler. When someone asks "is
it actually running?", the honest answer needs evidence, not assumptions.

Each loop calls :func:`record_run` after a successful iteration, writing a
timestamp (+ optional summary) to a tiny ``system_status`` table. The
automation-status endpoint reads these back so the UI can show "auto-paper
last ran 3 min ago" instead of leaving the operator guessing. Process-local
persistence in SQLite, so it survives a restart and reflects real activity.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite

from app.database import DB_PATH

logger = logging.getLogger(__name__)

_CREATE = """
CREATE TABLE IF NOT EXISTS system_status (
    name TEXT PRIMARY KEY,
    last_run_at TEXT NOT NULL,
    summary TEXT
)
"""


async def record_run(name: str, *, summary: Optional[dict[str, Any]] = None) -> None:
    """Stamp ``name``'s last-run time as now (UTC). Best-effort — never raises."""
    try:
        ts = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(summary) if summary is not None else None
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(_CREATE)
            await db.execute(
                """INSERT INTO system_status (name, last_run_at, summary)
                   VALUES (?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       last_run_at = excluded.last_run_at,
                       summary = excluded.summary""",
                (name, ts, payload),
            )
            await db.commit()
    except Exception as e:
        logger.debug("runtime_status.record_run(%s) failed: %s", name, e)


async def get_status() -> dict[str, dict[str, Any]]:
    """Return ``{name: {last_run_at, summary}}`` for every recorded loop."""
    out: dict[str, dict[str, Any]] = {}
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(_CREATE)
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT name, last_run_at, summary FROM system_status"
            ) as cur:
                async for r in cur:
                    summary = None
                    if r["summary"]:
                        try:
                            summary = json.loads(r["summary"])
                        except Exception:
                            summary = None
                    out[r["name"]] = {"last_run_at": r["last_run_at"], "summary": summary}
    except Exception as e:
        logger.debug("runtime_status.get_status failed: %s", e)
    return out

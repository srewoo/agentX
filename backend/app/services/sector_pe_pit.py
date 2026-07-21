from __future__ import annotations
"""3.5 — Point-in-time sector-PE snapshots.

The QV screener judges a stock's PE against its sector's median. Today that
median is a SINGLE current snapshot (``_build_sector_pe_lookup``) applied across
all history — so a 2021 backtest is scored against 2025 sector valuations, a
look-ahead the backtest never flags. There is no free history of sector-PE
medians, so this can't be back-filled; the honest fix is to START PERSISTING a
timestamped snapshot every time the lookup is built, so backtests going forward
resolve the median as-of the entry date.

  * ``save_snapshot`` — persist ``{sector: median_pe}`` stamped with a date.
  * ``get_sector_pe_at`` — the most recent snapshot median on/before an asof
    date (None when the store has nothing that old — caller falls back to the
    current lookup and should flag the residual bias).

The as-of picker is pure so it unit-tests without a DB.
"""
import logging
from datetime import date, datetime, timezone
from typing import Optional

import aiosqlite

from app.database import DB_PATH

logger = logging.getLogger(__name__)

CREATE_SECTOR_PE_TABLE = """
CREATE TABLE IF NOT EXISTS sector_pe_snapshots (
    snapshot_date TEXT NOT NULL,
    sector TEXT NOT NULL,
    median_pe REAL NOT NULL,
    PRIMARY KEY (snapshot_date, sector)
);
"""


def pick_asof(snapshots: list[tuple[str, float]], asof: date) -> Optional[float]:
    """Pure: most recent ``median_pe`` whose snapshot_date ≤ ``asof``.

    ``snapshots`` is ``[(iso_date, median_pe), ...]``; order-independent.
    Returns None when every snapshot is newer than ``asof`` (no PIT value yet).
    """
    best_d: Optional[str] = None
    best_v: Optional[float] = None
    target = asof.isoformat()
    for d, v in snapshots:
        if d <= target and (best_d is None or d > best_d):
            best_d, best_v = d, v
    return best_v


async def _ensure(db: aiosqlite.Connection) -> None:
    await db.execute(CREATE_SECTOR_PE_TABLE)


async def save_snapshot(
    lookup: dict[str, float], *, asof: Optional[date] = None, db_path: Optional[str] = None,
) -> int:
    """Persist a sector→median-PE snapshot for ``asof`` (today if None)."""
    if not lookup:
        return 0
    path = db_path or DB_PATH
    day = (asof or datetime.now(timezone.utc).date()).isoformat()
    async with aiosqlite.connect(path) as db:
        await _ensure(db)
        for sector, pe in lookup.items():
            if pe and pe > 0:
                await db.execute(
                    "INSERT OR REPLACE INTO sector_pe_snapshots "
                    "(snapshot_date, sector, median_pe) VALUES (?,?,?)",
                    (day, (sector or "").lower().strip(), float(pe)),
                )
        await db.commit()
    return len(lookup)


async def get_sector_pe_at(
    sector: str, asof: date, *, db_path: Optional[str] = None,
) -> Optional[float]:
    """Most recent snapshot median PE for ``sector`` on/before ``asof``."""
    path = db_path or DB_PATH
    sec = (sector or "").lower().strip()
    if not sec:
        return None
    try:
        async with aiosqlite.connect(path) as db:
            await _ensure(db)
            async with db.execute(
                "SELECT snapshot_date, median_pe FROM sector_pe_snapshots "
                "WHERE sector = ? AND snapshot_date <= ? "
                "ORDER BY snapshot_date DESC LIMIT 1", (sec, asof.isoformat())
            ) as cur:
                row = await cur.fetchone()
                return float(row[1]) if row else None
    except Exception as e:
        logger.debug("get_sector_pe_at failed: %s", e)
        return None


async def has_snapshot_before(asof: date, *, db_path: Optional[str] = None) -> bool:
    """True if any snapshot exists on/before ``asof`` (PIT resolution possible)."""
    path = db_path or DB_PATH
    try:
        async with aiosqlite.connect(path) as db:
            await _ensure(db)
            async with db.execute(
                "SELECT 1 FROM sector_pe_snapshots WHERE snapshot_date <= ? LIMIT 1",
                (asof.isoformat(),)
            ) as cur:
                return (await cur.fetchone()) is not None
    except Exception:
        return False

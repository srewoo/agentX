from __future__ import annotations
"""1.2 — Final untouched holdout.

The most recent ``HOLDOUT_MONTHS`` of history are RESERVED: no selection
decision (walk-forward that feeds gating, FDR promotion/mute, weight tuning,
symbol blocklisting) is allowed to read a bar dated after the holdout boundary.
The reserved window stays pristine for a single, final referee run at the end
of Phase 2 — a genuine out-of-sample verdict, not another surface the loop can
overfit to.

Design goals:

  * **Pinned, not rolling.** A rolling "last 12 months" boundary would creep
    forward every week, so each week's selection would quietly consume what was
    reserved. The boundary is pinned ONCE (``pin_boundary``) and is immutable
    thereafter — later pins are refused.
  * **Safe before it's pinned.** Until a boundary is pinned, enforcement is a
    logged no-op, so this module can ship before the reservation date is chosen.
  * **Referee escape hatch.** ``referee=True`` reads the full history including
    the holdout — used exactly once, deliberately, for the final verdict.

Enforcement point: every selection path fetches history then calls
``trim_history(df, boundary, referee=...)`` before scanning. Pure trim logic is
separated from the async settings/env resolution so it unit-tests cleanly.
"""
import logging
import os
from datetime import date, datetime, timezone
from typing import Optional

import aiosqlite

from app.database import DB_PATH

logger = logging.getLogger(__name__)

HOLDOUT_MONTHS = 12
_ENV_BOUNDARY = "AGENTX_HOLDOUT_BOUNDARY"   # ISO date override (highest priority)
_SETTINGS_KEY = "holdout_boundary_date"     # pinned reservation boundary

# Process cache so a 200-symbol run doesn't hit settings 200×. Cleared by
# pin_boundary so a fresh pin is visible without a restart.
_cache: dict[str, Optional[str]] = {}


def _months_before(anchor: date, months: int) -> date:
    """The date ``months`` calendar months before ``anchor`` (clamped day)."""
    m = anchor.month - 1 - months
    year = anchor.year + m // 12
    month = m % 12 + 1
    # Clamp day to the last valid day of the target month (28 is always safe).
    day = min(anchor.day, 28)
    return date(year, month, day)


def boundary_for_reservation(reservation: date, months: int = HOLDOUT_MONTHS) -> date:
    """Holdout boundary if the reservation is made on ``reservation``.

    Bars strictly AFTER the returned date are the reserved holdout.
    """
    return _months_before(reservation, months)


def _parse(v: object) -> Optional[date]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).strip()).date()
    except ValueError:
        try:
            return date.fromisoformat(str(v).strip())
        except ValueError:
            return None


async def resolve_boundary(*, db_path: Optional[str] = None) -> Optional[date]:
    """The pinned holdout boundary date, or None if not pinned (enforcement off).

    Priority: ``AGENTX_HOLDOUT_BOUNDARY`` env var → settings row → None.
    """
    env = os.environ.get(_ENV_BOUNDARY)
    if env:
        return _parse(env)
    path = db_path or DB_PATH
    if path in _cache:
        return _parse(_cache[path])
    val: Optional[str] = None
    try:
        async with aiosqlite.connect(path) as db:
            async with db.execute(
                "SELECT value FROM settings WHERE key = ?", (_SETTINGS_KEY,)
            ) as cur:
                row = await cur.fetchone()
                val = row[0] if row else None
    except Exception as e:
        logger.debug("holdout boundary lookup failed: %s", e)
    _cache[path] = val
    return _parse(val)


async def pin_boundary(
    *, today: Optional[date] = None, months: int = HOLDOUT_MONTHS,
    db_path: Optional[str] = None,
) -> dict:
    """Pin the holdout boundary to ``today − months``. Immutable once set.

    Returns the pinned boundary + whether this call created it. A second call
    is a no-op (the existing pin is returned) so the reserved window can never
    be silently moved after data has been seen.
    """
    path = db_path or DB_PATH
    existing = await resolve_boundary(db_path=path)
    if existing is not None:
        return {"boundary": existing.isoformat(), "created": False,
                "reason": "already pinned — immutable"}
    anchor = today or datetime.now(timezone.utc).date()
    boundary = _months_before(anchor, months)
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (_SETTINGS_KEY, boundary.isoformat()),
        )
        await db.commit()
    _cache.pop(path, None)
    return {"boundary": boundary.isoformat(), "created": True,
            "reservation_date": anchor.isoformat(), "months": months}


def trim_history(df, boundary: Optional[date], *, referee: bool = False):
    """Drop bars strictly after ``boundary`` unless ``referee``.

    No-op when the boundary is None (unpinned) or referee mode is on. The input
    DataFrame is expected to have a DatetimeIndex; anything else is returned
    unchanged (fail-open — never crash a backtest over the guard itself).
    """
    if referee or boundary is None or df is None or len(df) == 0:
        return df
    try:
        import pandas as pd

        idx = pd.DatetimeIndex(pd.to_datetime(df.index)).tz_localize(None)
        cutoff = pd.Timestamp(boundary)
        import numpy as np

        mask = np.asarray(idx <= cutoff)
        if mask.all():
            return df
        trimmed = df[mask]
        logger.debug(
            "holdout: trimmed %d bars after %s (%d kept)",
            len(df) - len(trimmed), boundary.isoformat(), len(trimmed),
        )
        return trimmed
    except Exception as e:
        logger.warning("holdout trim skipped (non-fatal): %s", e)
        return df


def _clear_cache() -> None:
    """Test hook: drop the resolved-boundary cache."""
    _cache.clear()

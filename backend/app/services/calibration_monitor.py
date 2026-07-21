from __future__ import annotations
"""4.1 — Calibration as a first-class weekly metric with drift alerting.

``calibration_curve`` already fits conviction→p(win) and computes per-decile
reliability bins. What was missing is TIME: a weekly snapshot history and an
alert when calibration DRIFTS — the specific failure the plan calls out,
"predicted 57% delivering <50% for two consecutive weeks." A model that was
calibrated at deploy can rot as the regime turns; without a weekly check nobody
notices until the forward verdict is already poisoned.

This module snapshots the reliability diagram each week and fires when a
high-conviction bin has under-delivered for ``CONSECUTIVE_WEEKS`` running.

The drift logic is pure (``bin_miscalibrated`` / ``is_drifting``) so it
unit-tests without a DB.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite

from app.database import DB_PATH

logger = logging.getLogger(__name__)

# "predicted 57% delivering <50%": a bin whose mean predicted p(win) is at least
# PREDICTED_FLOOR but whose realized win rate is below REALIZED_FLOOR is
# over-confident. Needs MIN_BIN_N samples so a 1-trade bin can't trip it.
PREDICTED_FLOOR = 0.57
REALIZED_FLOOR = 0.50
MIN_BIN_N = 10
CONSECUTIVE_WEEKS = 2

CREATE_CALIBRATION_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS calibration_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_at TEXT NOT NULL,
    samples INTEGER,
    brier_calibrated REAL,
    miscalibrated INTEGER NOT NULL,   -- 1 if any high-conviction bin under-delivered
    report_json TEXT
);
"""


def bin_miscalibrated(
    reliability: list[dict[str, Any]], *, predicted_floor: float = PREDICTED_FLOOR,
    realized_floor: float = REALIZED_FLOOR, min_n: int = MIN_BIN_N,
) -> list[dict[str, Any]]:
    """Bins that are over-confident: predicted ≥ floor, realized < floor, n ≥ min_n."""
    return [
        b for b in reliability
        if (b.get("n", 0) >= min_n
            and float(b.get("predicted", 0)) >= predicted_floor
            and float(b.get("realized", 0)) < realized_floor)
    ]


def is_drifting(week_flags: list[bool], consecutive: int = CONSECUTIVE_WEEKS) -> bool:
    """True when the most recent ``consecutive`` weekly snapshots were ALL
    miscalibrated. ``week_flags`` is chronological (oldest → newest)."""
    if len(week_flags) < consecutive:
        return False
    return all(week_flags[-consecutive:])


async def _ensure(db: aiosqlite.Connection) -> None:
    await db.execute(CREATE_CALIBRATION_HISTORY_TABLE)


async def record_and_check(
    report: dict[str, Any], *, db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Persist this week's snapshot and evaluate drift over recent weeks.

    ``report`` is a ``calibration_curve.build_calibration_curve`` result. Returns
    a verdict dict; ``drifting=True`` means alert."""
    path = db_path or DB_PATH
    if report.get("status") != "ok":
        return {"status": report.get("status", "unknown"), "drifting": False}

    reliability = report.get("reliability", [])
    offending = bin_miscalibrated(reliability)
    miscalibrated = bool(offending)
    now = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(path) as db:
        await _ensure(db)
        await db.execute(
            "INSERT INTO calibration_history (snapshot_at, samples, brier_calibrated, "
            "miscalibrated, report_json) VALUES (?,?,?,?,?)",
            (now, report.get("samples"), report.get("brier_calibrated"),
             int(miscalibrated), json.dumps({"reliability": reliability})),
        )
        await db.commit()
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT miscalibrated FROM calibration_history ORDER BY id DESC LIMIT ?",
            (CONSECUTIVE_WEEKS,)
        ) as cur:
            recent = [bool(r["miscalibrated"]) for r in await cur.fetchall()][::-1]

    drifting = is_drifting(recent)
    verdict = {
        "status": "ok",
        "miscalibrated_this_week": miscalibrated,
        "offending_bins": offending,
        "consecutive_weeks_checked": len(recent),
        "drifting": drifting,
    }
    if drifting:
        logger.warning(
            "CALIBRATION DRIFT: high-conviction bins under-delivered for %d "
            "consecutive weeks — predicted ≥%.0f%% realizing <%.0f%%. Bins: %s",
            CONSECUTIVE_WEEKS, PREDICTED_FLOOR * 100, REALIZED_FLOOR * 100, offending)
    return verdict


async def run_weekly_calibration_check(*, db_path: Optional[str] = None) -> dict[str, Any]:
    """Build this week's calibration curve, snapshot it, and check for drift."""
    from app.services.calibration_curve import build_calibration_curve
    report = await build_calibration_curve(db_path=db_path)
    verdict = await record_and_check(report, db_path=db_path)
    return {"calibration": report.get("status"), **verdict}

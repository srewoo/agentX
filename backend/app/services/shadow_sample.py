from __future__ import annotations
"""4.3 — Bound the selection bias with a shadow sample of rejected signals.

Both meta models train ONLY on engine-selected trades, so they learn "was the
engine right about what it took?" — never "what did the engine wrongly discard?"
That leaves the funnel's selection bias unmeasured: if the engine rejects
winners, no metric ever notices.

This logs a random ~5% sample of REJECTED candidates as shadow trades (never
real capital), simulates their stop/target/time outcome on subsequent bars using
the SAME gap-aware exit model as the backtest, and reports the discarded-vs-taken
win rate. The gap between them is the measured selection-bias bound.

Sampling is a stable HASH of (symbol, date, reason), not RNG — so it's
deterministic and reproducible (the same rejection is always in or out of the
sample), which also makes it unit-testable.
"""
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite

from app.database import DB_PATH

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_RATE = 0.05
_MAX_HOLD_BARS = 7

CREATE_SHADOW_REJECTS_TABLE = """
CREATE TABLE IF NOT EXISTS shadow_rejects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price REAL NOT NULL,
    stop_loss REAL, target REAL,
    reason TEXT,
    rejected_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | closed
    outcome TEXT,                              -- win | loss
    pnl_pct REAL, exit_reason TEXT, evaluated_at TEXT
);
"""


def should_sample(key: str, rate: float = DEFAULT_SAMPLE_RATE) -> bool:
    """Deterministic ~``rate`` sampler: stable hash of ``key`` → uniform [0,1).

    Same key → same decision every time (reproducible, testable), while across
    many distinct keys the inclusion fraction ≈ ``rate``."""
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()
    frac = int(h[:8], 16) / 0xFFFFFFFF
    return frac < rate


def simulate_shadow_outcome(
    direction: str, entry: float, stop: float, target: float, future_bars,
) -> Optional[dict[str, Any]]:
    """Simulate the exit of a rejected candidate over ``future_bars`` (dicts with
    open/high/low/close), gap-aware. Returns ``{pnl_pct, exit_reason}`` or None if
    no bars. Reuses the backtest exit model so shadow and real use one definition."""
    from app.services.backtester_walk_forward import _simulate_path_exit
    if not future_bars or entry <= 0:
        return None
    highs = [float(b["high"]) for b in future_bars]
    lows = [float(b["low"]) for b in future_bars]
    closes = [float(b["close"]) for b in future_bars]
    opens = [float(b["open"]) for b in future_bars]
    exit_px, reason, _ = _simulate_path_exit(
        direction, entry, stop, target,
        highs[:_MAX_HOLD_BARS], lows[:_MAX_HOLD_BARS], closes[:_MAX_HOLD_BARS],
        opens[:_MAX_HOLD_BARS])
    sign = 1.0 if direction == "bullish" else -1.0
    pnl = (exit_px - entry) / entry * 100.0 * sign
    return {"pnl_pct": round(pnl, 4), "exit_reason": reason}


async def _ensure(db: aiosqlite.Connection) -> None:
    await db.execute(CREATE_SHADOW_REJECTS_TABLE)


async def log_shadow_reject(
    *, symbol: str, direction: str, entry: float, stop: Optional[float],
    target: Optional[float], reason: str, rejected_at: Optional[str] = None,
    rate: float = DEFAULT_SAMPLE_RATE, db_path: Optional[str] = None,
) -> bool:
    """Log a rejected candidate as a shadow trade IF it falls in the sample.
    Returns True when logged. Fire-and-forget: never raises into the trade path."""
    at = rejected_at or datetime.now(timezone.utc).isoformat()
    if not should_sample(f"{symbol}|{direction}|{at[:10]}|{reason}", rate):
        return False
    try:
        path = db_path or DB_PATH
        async with aiosqlite.connect(path) as db:
            await _ensure(db)
            await db.execute(
                "INSERT INTO shadow_rejects (symbol, direction, entry_price, "
                "stop_loss, target, reason, rejected_at) VALUES (?,?,?,?,?,?,?)",
                (symbol, direction, float(entry), stop, target, reason, at),
            )
            await db.commit()
        return True
    except Exception as e:
        logger.debug("shadow reject log skipped for %s: %s", symbol, e)
        return False


def bias_bound(taken_win_rate: float, shadow_win_rate: float) -> dict[str, Any]:
    """The measured selection-bias bound: taken vs discarded win rate.

    ``discarded_edge_gap`` > 0 means the engine discarded trades that (on this
    sample) won MORE often than what it took — a funnel that's leaving edge on
    the table. ≤ 0 means the funnel is correctly shedding weaker setups."""
    gap = round(shadow_win_rate - taken_win_rate, 4)
    return {
        "taken_win_rate": round(taken_win_rate, 4),
        "shadow_reject_win_rate": round(shadow_win_rate, 4),
        "discarded_edge_gap": gap,
        "interpretation": (
            "funnel discarded winners — selection may be too tight"
            if gap > 0.05 else
            "funnel correctly shed weaker setups" if gap < -0.05 else
            "taken and discarded perform similarly — selection adds little"
        ),
    }


async def bias_report(*, db_path: Optional[str] = None) -> dict[str, Any]:
    """Compare taken-trade win rate (paper_trades) vs shadow-rejected win rate."""
    path = db_path or DB_PATH
    async with aiosqlite.connect(path) as db:
        await _ensure(db)
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT COUNT(*) n, SUM(CASE WHEN pnl_pct>0 THEN 1 ELSE 0 END) w "
            "FROM paper_trades WHERE status='closed' AND pnl_pct IS NOT NULL"
        ) as cur:
            row = await cur.fetchone()
            taken_n, taken_w = int(row["n"] or 0), int(row["w"] or 0)
        async with db.execute(
            "SELECT COUNT(*) n, SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) w "
            "FROM shadow_rejects WHERE status='closed'"
        ) as cur:
            row = await cur.fetchone()
            sh_n, sh_w = int(row["n"] or 0), int(row["w"] or 0)
    taken_wr = taken_w / taken_n if taken_n else 0.0
    sh_wr = sh_w / sh_n if sh_n else 0.0
    return {
        "taken_n": taken_n, "shadow_n": sh_n,
        "caveat": ("shadow sample is a random ~5% of REJECTED candidates, "
                   "outcomes simulated on subsequent bars — bounds the selection "
                   "bias the meta-models can't see (they train on taken trades only)"),
        **bias_bound(taken_wr, sh_wr),
    }

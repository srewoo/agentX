from __future__ import annotations
"""Realistic execution-cost models (Almgren-Chriss square-root market impact)
and point-in-time (PIT) fundamentals snapshotting.

Two problems with the prior 20-bp flat transaction cost:

  • It's optimistic for small-caps where a 1% ADV order can cost 40-60 bp
    in market impact alone (Almgren et al. 2005, "Direct Estimation of
    Equity Market Impact").
  • It's pessimistic for the most liquid NIFTY names where round-trip
    after broker rebates can be 8-12 bp.

We add a per-trade cost model and a fundamentals snapshot table so the
walk-forward can't accidentally use *restated* numbers from the future.
"""
import logging
import math
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite

from app.database import DB_PATH

logger = logging.getLogger(__name__)


# Indian-market constants. Round-trip = brokerage + STT + SEBI + stamp.
_FIXED_FEES_BPS = 12.0  # ~0.12% combined fixed cost (broker + statutory)


def sqrt_impact_cost_bps(
    *,
    trade_value_inr: float,
    avg_daily_value_inr: float,
    daily_vol_pct: float,
    fixed_fees_bps: float = _FIXED_FEES_BPS,
    impact_coeff: float = 0.6,
) -> dict[str, float]:
    """Almgren-Chriss-style square-root market impact, in bps.

    cost_bps = fixed_fees + impact_coeff × daily_vol_pct × 100 × sqrt(participation)

    where `participation = trade_value / avg_daily_value`.

    Calibration: `impact_coeff` ~0.5-0.6 fits empirical NSE intraday slippage
    (cf. Frino et al. 2015 on Asia-Pacific markets).
    """
    if trade_value_inr <= 0 or avg_daily_value_inr <= 0:
        return {"total_bps": fixed_fees_bps, "impact_bps": 0.0, "fixed_bps": fixed_fees_bps}
    participation = trade_value_inr / avg_daily_value_inr
    impact_bps = impact_coeff * daily_vol_pct * 100 * math.sqrt(max(0.0, participation))
    return {
        "total_bps": round(fixed_fees_bps + impact_bps, 2),
        "impact_bps": round(impact_bps, 2),
        "fixed_bps": round(fixed_fees_bps, 2),
        "participation_pct": round(participation * 100, 3),
    }


def round_trip_cost_pct(
    *,
    trade_value_inr: float,
    avg_daily_value_inr: float,
    daily_vol_pct: float,
) -> float:
    """Round-trip (enter + exit) cost as a percentage of trade value.

    Returns 0.20 for a tiny trade on a hyper-liquid stock; up to ~1.0+
    for a 5% ADV order on a thin name. Use this where the backtester
    previously hard-coded 0.20.
    """
    one_way = sqrt_impact_cost_bps(
        trade_value_inr=trade_value_inr,
        avg_daily_value_inr=avg_daily_value_inr,
        daily_vol_pct=daily_vol_pct,
    )
    return round(one_way["total_bps"] * 2 / 100.0, 4)


# ── PIT fundamentals snapshots ──────────────────────────────────────────

async def _ensure_pit_table() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS fundamentals_pit (
                symbol TEXT NOT NULL,
                as_of_date TEXT NOT NULL,
                source TEXT NOT NULL,
                fundamentals_json TEXT NOT NULL,
                composite_score INTEGER,
                created_at TEXT NOT NULL,
                PRIMARY KEY (symbol, as_of_date)
            )"""
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fundpit_symbol_date ON fundamentals_pit(symbol, as_of_date)"
        )
        await db.commit()


async def snapshot_fundamentals(
    symbol: str,
    fundamentals: dict[str, Any],
    *,
    source: str = "yfinance",
    composite_score: Optional[int] = None,
) -> None:
    """Persist today's fundamentals snapshot for `symbol`.

    The backtester should call `load_fundamentals_as_of(symbol, t)`
    instead of fetching fresh yfinance numbers — yfinance returns
    *current restated* financials, which leak future-knowledge into
    historical bars.
    """
    await _ensure_pit_table()
    import json
    as_of = datetime.now(timezone.utc).date().isoformat()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT OR REPLACE INTO fundamentals_pit
                     (symbol, as_of_date, source, fundamentals_json,
                      composite_score, created_at)
                     VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    symbol, as_of, source, json.dumps(fundamentals, default=str),
                    int(composite_score) if composite_score is not None else None,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            await db.commit()
    except Exception as e:
        logger.debug("snapshot_fundamentals skipped for %s: %s", symbol, e)


async def load_fundamentals_as_of(symbol: str, as_of: str) -> Optional[dict[str, Any]]:
    """Return the most recent snapshot for `symbol` that is ≤ `as_of` (ISO date).

    Use this in backtests to avoid look-ahead bias. Returns None when
    no historical snapshot exists for the symbol on or before that date.
    """
    await _ensure_pit_table()
    import json
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT fundamentals_json, as_of_date, composite_score
                   FROM fundamentals_pit
                   WHERE symbol = ? AND as_of_date <= ?
                   ORDER BY as_of_date DESC LIMIT 1""",
                (symbol, as_of),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        data = json.loads(row["fundamentals_json"])
        data["_pit_as_of_date"] = row["as_of_date"]
        data["_pit_composite_score"] = row["composite_score"]
        return data
    except Exception as e:
        logger.debug("load_fundamentals_as_of failed for %s: %s", symbol, e)
        return None

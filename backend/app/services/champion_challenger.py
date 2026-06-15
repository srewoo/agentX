from __future__ import annotations
"""A4 — champion/challenger promotion with auto-rollback.

A newly-derived gating config (A1/A2) must not go live just because it looked
better in the backtest — that is exactly how overfitting ships. Instead it runs
as a CHALLENGER in shadow (scored on forward data, not traded) alongside the
live CHAMPION. The challenger is promoted to champion only when it beats the
incumbent on *forward* results by a margin and on an adequate sample; and a
live champion is rolled back if its forward expectancy falls below a floor.

The decision logic is pure (testable); persistence of the champion/challenger
records is a thin settings-table layer.
"""
import json
import logging
from typing import Any, Optional

import aiosqlite

from app.database import DB_PATH

logger = logging.getLogger(__name__)

_CHAMPION_KEY = "gating_champion"
_CHALLENGER_KEY = "gating_challenger"

PROMOTE_MARGIN_PCT = 0.10   # challenger expectancy must beat champion by ≥ this (pp/trade)
MIN_FORWARD_TRADES = 50     # both arms need this many forward trades to compare
ROLLBACK_FLOOR_PCT = -0.25  # champion expectancy below this (pp/trade) → roll back


def challenger_promotion_decision(
    champion: dict[str, Any],
    challenger: dict[str, Any],
    *,
    margin_pct: float = PROMOTE_MARGIN_PCT,
    min_trades: int = MIN_FORWARD_TRADES,
) -> dict[str, Any]:
    """Should the challenger replace the champion? Pure.

    Each arm dict carries ``expectancy_pct`` and ``trades``. Promote only when
    the challenger has enough forward trades AND beats the champion's
    expectancy by ``margin_pct``. Default is to keep the champion.
    """
    ch_n = int(challenger.get("trades", 0))
    champ_exp = float(champion.get("expectancy_pct", 0.0))
    ch_exp = float(challenger.get("expectancy_pct", 0.0))
    if ch_n < min_trades:
        return {"promote": False, "reason": f"challenger sample {ch_n} < {min_trades}"}
    if ch_exp >= champ_exp + margin_pct:
        return {"promote": True,
                "reason": f"challenger {ch_exp:.3f} beats champion {champ_exp:.3f} by ≥ {margin_pct}"}
    return {"promote": False,
            "reason": f"challenger {ch_exp:.3f} does not beat champion {champ_exp:.3f} by {margin_pct}"}


def rollback_decision(
    champion_live: dict[str, Any], *, floor_pct: float = ROLLBACK_FLOOR_PCT,
    min_trades: int = MIN_FORWARD_TRADES,
) -> dict[str, Any]:
    """Should the live champion be rolled back? Pure.

    Rolls back only on an adequate sample whose expectancy is below the floor —
    a few bad trades don't trigger a panic rollback.
    """
    n = int(champion_live.get("trades", 0))
    exp = float(champion_live.get("expectancy_pct", 0.0))
    if n < min_trades:
        return {"rollback": False, "reason": f"only {n} forward trades; hold"}
    if exp < floor_pct:
        return {"rollback": True, "reason": f"expectancy {exp:.3f} below floor {floor_pct}"}
    return {"rollback": False, "reason": f"expectancy {exp:.3f} above floor {floor_pct}"}


async def _get(key: str, db_path: str) -> Optional[dict]:
    try:
        async with aiosqlite.connect(db_path) as db:
            async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
                row = await cur.fetchone()
        return json.loads(row[0]) if row and row[0] else None
    except Exception:
        return None


async def _set(key: str, value: Optional[dict], db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        if value is None:
            await db.execute("DELETE FROM settings WHERE key=?", (key,))
        else:
            await db.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)))
        await db.commit()


async def set_challenger(config: dict, *, db_path: Optional[str] = None) -> None:
    """Register a newly-derived config as the shadow challenger."""
    await _set(_CHALLENGER_KEY, config, db_path or DB_PATH)


async def evaluate_and_maybe_promote(
    champion_metrics: dict[str, Any],
    challenger_metrics: dict[str, Any],
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Run the promotion + rollback decisions and persist the outcome.

    Returns the action taken: 'promoted' (challenger→champion), 'rolled_back'
    (champion cleared), or 'held'. Forward metrics for each arm are supplied by
    the caller (from the decision log / paper trades per config).
    """
    path = db_path or DB_PATH
    promo = challenger_promotion_decision(champion_metrics, challenger_metrics)
    if promo["promote"]:
        challenger = await _get(_CHALLENGER_KEY, path)
        if challenger is not None:
            await _set(_CHAMPION_KEY, challenger, path)
            await _set(_CHALLENGER_KEY, None, path)
        return {"action": "promoted", **promo}
    rb = rollback_decision(champion_metrics)
    if rb["rollback"]:
        await _set(_CHAMPION_KEY, None, path)  # fall back to hand-curated constants
        return {"action": "rolled_back", **rb}
    return {"action": "held", "promotion": promo, "rollback": rb}

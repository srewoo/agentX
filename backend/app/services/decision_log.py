from __future__ import annotations
"""Forward decision log (D1) — the honest forward record.

Every recommendation the auto-trader *considers* each cycle produces one row
here: taken or skipped, with the full decision-time snapshot (conviction,
measured p(win), sizing outcome, gating verdict, skip reason). Two reasons this
exists separately from ``recommendation_outcomes``:

  1. **Selection bias is the signal.** A backtest of only the trades we *took*
     is blind to the ones we passed on. To know whether the gates (Kelly,
     correlation, risk gate) help or hurt, we must log the rejected candidates
     and their reasons — `recommendation_outcomes` never sees them.
  2. **Point-in-time truth.** The snapshot is frozen at decision time, so later
     re-tuning of weights/edges can't retro-colour what we actually decided.

Append-only. Writes are **fire-and-forget**: a logging failure must never break
the trade path, so every public function swallows its own errors and returns a
count (0 on failure) rather than raising.
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite

from app.database import CREATE_DECISION_LOG_TABLE, CREATE_DECISION_LOG_INDEXES, DB_PATH

logger = logging.getLogger(__name__)

# Columns in insertion order. Single source of truth for the INSERT so the
# value tuple and the SQL can never drift apart.
_COLUMNS = (
    "id", "decided_at", "trade_date", "symbol", "horizon", "action",
    "direction", "conviction", "meta_label_prob", "entry", "stoploss",
    "target1", "risk_reward", "regime", "sector", "weighted_score",
    "factor_agreement", "taken", "skip_reason", "win_prob_used",
    "kelly_f_used", "payoff_ratio", "shares", "position_value",
    "binding_constraint", "max_correlation", "source", "factors_json",
)


def _row_from_record(rec: dict[str, Any], decided_at: str) -> tuple:
    """Project a loose decision dict onto the fixed column tuple.

    Tolerant by design: unknown keys are ignored, missing keys become NULL,
    so callers can pass whatever snapshot they have without tracking the
    schema. ``factors`` (a list/dict) is JSON-encoded into ``factors_json``.
    """
    factors = rec.get("factors")
    factors_json = None
    if factors is not None:
        try:
            factors_json = json.dumps(factors, default=str)[:20000]
        except Exception:
            factors_json = None
    values = {
        "id": rec.get("id") or uuid.uuid4().hex[:12],
        "decided_at": decided_at,
        "trade_date": rec.get("trade_date") or decided_at[:10],
        "symbol": rec.get("symbol"),
        "horizon": rec.get("horizon"),
        "action": rec.get("action"),
        "direction": rec.get("direction"),
        "conviction": rec.get("conviction"),
        "meta_label_prob": rec.get("meta_label_prob"),
        "entry": rec.get("entry"),
        "stoploss": rec.get("stoploss"),
        "target1": rec.get("target1"),
        "risk_reward": rec.get("risk_reward"),
        "regime": rec.get("regime"),
        "sector": rec.get("sector"),
        "weighted_score": rec.get("weighted_score"),
        "factor_agreement": rec.get("factor_agreement"),
        "taken": 1 if rec.get("taken") else 0,
        "skip_reason": rec.get("skip_reason"),
        "win_prob_used": rec.get("win_prob_used"),
        "kelly_f_used": rec.get("kelly_f_used"),
        "payoff_ratio": rec.get("payoff_ratio"),
        "shares": rec.get("shares"),
        "position_value": rec.get("position_value"),
        "binding_constraint": rec.get("binding_constraint"),
        "max_correlation": rec.get("max_correlation"),
        "source": rec.get("source") or "auto",
        "factors_json": factors_json,
    }
    return tuple(values[c] for c in _COLUMNS)


async def log_decisions(
    records: list[dict[str, Any]], *, db_path: Optional[str] = None
) -> int:
    """Append a batch of decision records. Returns rows written (0 on failure).

    Fire-and-forget: never raises into the caller. Creates the table on first
    use so the logger is self-sufficient even if init_db hasn't run (e.g. in
    tests or a fresh install).
    """
    if not records:
        return 0
    path = db_path or DB_PATH
    decided_at = datetime.now(timezone.utc).isoformat()
    placeholders = ", ".join(["?"] * len(_COLUMNS))
    sql = f"INSERT INTO decision_log ({', '.join(_COLUMNS)}) VALUES ({placeholders})"
    try:
        rows = [_row_from_record(r, decided_at) for r in records]
        async with aiosqlite.connect(path) as db:
            await db.execute(CREATE_DECISION_LOG_TABLE)
            for idx_sql in CREATE_DECISION_LOG_INDEXES:
                await db.execute(idx_sql)
            await db.executemany(sql, rows)
            await db.commit()
        return len(rows)
    except Exception as e:  # never break the trade path on a logging failure
        logger.warning("decision_log write skipped (%d records): %s", len(records), e)
        return 0


async def recent_decisions(
    *, limit: int = 200, taken_only: bool = False, db_path: Optional[str] = None
) -> list[dict[str, Any]]:
    """Read recent decisions, newest first. Returns [] on any failure.

    Feeds the benchmark-relative reporting (D2) and durability check (D4).
    """
    path = db_path or DB_PATH
    where = "WHERE taken = 1" if taken_only else ""
    sql = f"SELECT * FROM decision_log {where} ORDER BY decided_at DESC LIMIT ?"
    try:
        async with aiosqlite.connect(path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (int(limit),)) as cur:
                return [dict(r) for r in await cur.fetchall()]
    except Exception as e:
        logger.debug("recent_decisions read failed: %s", e)
        return []


async def decision_summary(*, db_path: Optional[str] = None) -> dict[str, Any]:
    """Aggregate counts for a quick health/readiness view.

    Returns total considered, taken, skipped, and a skip-reason breakdown —
    the raw inputs for the D3 readiness gate ("n taken / target").
    """
    path = db_path or DB_PATH
    try:
        async with aiosqlite.connect(path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT COUNT(*) AS n, "
                "SUM(CASE WHEN taken = 1 THEN 1 ELSE 0 END) AS taken "
                "FROM decision_log"
            ) as cur:
                row = dict(await cur.fetchone() or {})
            total = int(row.get("n") or 0)
            taken = int(row.get("taken") or 0)
            reasons: dict[str, int] = {}
            async with db.execute(
                "SELECT skip_reason, COUNT(*) AS c FROM decision_log "
                "WHERE taken = 0 AND skip_reason IS NOT NULL GROUP BY skip_reason"
            ) as cur:
                for r in await cur.fetchall():
                    reasons[str(r["skip_reason"])] = int(r["c"])
        return {"considered": total, "taken": taken, "skipped": total - taken,
                "skip_reasons": reasons}
    except Exception as e:
        logger.debug("decision_summary failed: %s", e)
        return {"considered": 0, "taken": 0, "skipped": 0, "skip_reasons": {}}

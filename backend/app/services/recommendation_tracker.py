from __future__ import annotations
"""Closed-loop self-improvement for the multi-factor recommendation engine.

  store_recommendation()              ↘
                                       → recommendation_outcomes table
  evaluate_recommendation_outcomes()  ↗

  _recalculate_factor_performance()   → factor_performance table
                                       + _factor_edge_cache (in-memory)

  factor_edge_multiplier(factor)      ← read by recommendation._score_all
                                       to scale each factor's weight
                                       toward proven winners (clamped 0.5..1.5)

The signal_engine has the same loop for its own signal types — this module
is the equivalent for the new 10-factor engine. Same cron cadence, same
clamps, so the two paths can co-exist without one dominating the other.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiosqlite

from app.database import DB_PATH
from app.services.data_fetcher import async_fetch_history

logger = logging.getLogger(__name__)

# Same clamp range as signal_engine's dynamic weighting — keeps the two
# paths comparable and prevents any single factor from dominating.
_WEIGHT_MIN = 0.5
_WEIGHT_MAX = 1.5

# A factor must have at least this many directional recommendations behind
# it before its edge actually changes its weight. Below the threshold we
# return 1.0 (no adjustment) so noisy early-day stats don't drive trading.
_MIN_SAMPLE_SIZE = 20

# In-memory cache populated by `_recalculate_factor_performance` and
# `seed_factor_edge_cache`. Format: {factor_name: edge_in_pct_pnl}.
_factor_edge_cache: dict[str, float] = {}

_TRACKER_COLUMNS: dict[str, str] = {
    "max_favorable_pct": "REAL",
    "max_adverse_pct": "REAL",
    "bars_held": "INTEGER",
    "outcome_reason": "TEXT",
    "regime": "TEXT",
    "weighted_score": "REAL",
    "factor_agreement": "REAL",
    "data_quality": "TEXT",
    # tracked=0 means the engine considered the rec but action was HOLD/AVOID;
    # outcome evaluator skips these, but the cohort dashboard counts them so
    # we can see what the engine almost-took.
    "tracked": "INTEGER DEFAULT 1",
}


async def _ensure_tracker_columns() -> None:
    """Add tracker columns for existing SQLite DBs.

    Existing installs may already have `recommendation_outcomes` without the
    newer calibration/outcome columns. SQLite lacks a portable IF NOT EXISTS
    for ADD COLUMN on the supported local version, so we inspect PRAGMA first.
    """
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("PRAGMA table_info(recommendation_outcomes)") as cur:
                rows = await cur.fetchall()
            existing = {row[1] for row in rows}
            for name, ddl in _TRACKER_COLUMNS.items():
                if name not in existing:
                    await db.execute(f"ALTER TABLE recommendation_outcomes ADD COLUMN {name} {ddl}")
            await db.commit()
    except Exception as e:
        logger.debug("tracker column migration skipped: %s", e)


# ── persist a recommendation when generated ─────────────────────────────

async def store_recommendation(rec) -> None:
    """Persist a `Recommendation` for later outcome evaluation.

    Called from `recommendation.generate_recommendation` once we know the
    rec is directional (BUY or SELL). HOLD / AVOID are skipped — they have
    no entry to evaluate and would drag the dataset.
    """
    if rec is None:
        return
    if rec.action not in ("BUY", "SELL", "HOLD", "AVOID"):
        return
    tracked = 1 if rec.action in ("BUY", "SELL") else 0
    await _ensure_tracker_columns()
    rec_id = f"{rec.symbol}:{rec.horizon}:{rec.generated_at.isoformat()}"
    signals_json = json.dumps([
        {"name": s.name, "weight": s.weight, "score": s.score, "direction": s.direction}
        for s in (rec.signals or [])
    ])
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT OR IGNORE INTO recommendation_outcomes
                   (rec_id, symbol, horizon, action, conviction, entry, stoploss,
                    target1, timeframe_days, signals_json, sector, created_at,
                    regime, weighted_score, factor_agreement, data_quality, tracked)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    rec_id, rec.symbol, rec.horizon, rec.action, rec.conviction,
                    float(rec.entry), float(rec.stoploss), float(rec.target1),
                    int(rec.timeframe_days), signals_json, rec.sector,
                    rec.generated_at.isoformat(),
                    rec.regime, rec.weighted_score, rec.factor_agreement, rec.data_quality,
                    tracked,
                ),
            )
            await db.commit()
    except Exception as e:
        # Tracker failures must not break the recommendation surface.
        logger.debug("store_recommendation failed for %s: %s", rec.symbol, e)


# ── evaluate unresolved outcomes ────────────────────────────────────────

async def evaluate_recommendation_outcomes() -> dict[str, Any]:
    """Walk unresolved recommendations and decide win / loss / expired.

    Outcome rules (mirror signal_tracker's win/loss semantics):
      - BUY: hit target1 → win, hit stoploss → loss
      - SELL: hit stoploss (price moved up to SL) → loss; hit target1 → win
      - Neither hit by `entry_time + timeframe_days` → expired (P&L is
        the close-to-close return).

    Returns a small summary for observability.
    """
    cutoff_now = datetime.now(timezone.utc)
    await _ensure_tracker_columns()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT rec_id, symbol, action, entry, stoploss, target1,
                      timeframe_days, created_at
               FROM recommendation_outcomes
               WHERE outcome IS NULL
                 AND COALESCE(tracked, 1) = 1
                 AND action IN ('BUY','SELL')"""
        ) as cur:
            unresolved = await cur.fetchall()

    if not unresolved:
        return {"evaluated": 0, "wins": 0, "losses": 0, "expired": 0}

    # Group by symbol so we fetch each price history at most once.
    by_symbol: dict[str, list[dict]] = {}
    for r in unresolved:
        by_symbol.setdefault(r["symbol"], []).append(dict(r))

    wins = losses = expired = 0
    updates: list[dict] = []

    for sym, recs in by_symbol.items():
        try:
            df = await async_fetch_history(sym, period="3mo", interval="1d")
        except Exception as e:
            logger.debug("history fetch failed for %s: %s", sym, e)
            continue
        if df is None or df.empty:
            continue

        for r in recs:
            try:
                created = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
            except Exception:
                continue
            horizon_end = created + timedelta(days=int(r["timeframe_days"]))

            # Slice bars from creation date forward.
            bars = df[df.index >= created.replace(tzinfo=None) if df.index.tz is None else df.index >= created]
            if bars.empty:
                continue

            outcome = exit_price = exit_time = None
            outcome_reason = None
            entry = float(r["entry"])
            sl = float(r["stoploss"])
            tgt = float(r["target1"])
            max_fav = 0.0
            max_adv = 0.0
            bars_held = 0

            for ts, row in bars.iterrows():
                hi = float(row.get("High") or row.get("high") or row.get("Close"))
                lo = float(row.get("Low") or row.get("low") or row.get("Close"))
                bars_held += 1
                if r["action"] == "BUY":
                    max_fav = max(max_fav, (hi - entry) / entry * 100.0)
                    max_adv = min(max_adv, (lo - entry) / entry * 100.0)
                    if lo <= sl:
                        outcome, exit_price = "loss", sl
                        outcome_reason = "stoploss_hit"
                        exit_time = str(ts); break
                    if hi >= tgt:
                        outcome, exit_price = "win", tgt
                        outcome_reason = "target_hit"
                        exit_time = str(ts); break
                else:  # SELL
                    max_fav = max(max_fav, (entry - lo) / entry * 100.0)
                    max_adv = min(max_adv, (entry - hi) / entry * 100.0)
                    if hi >= sl:
                        outcome, exit_price = "loss", sl
                        outcome_reason = "stoploss_hit"
                        exit_time = str(ts); break
                    if lo <= tgt:
                        outcome, exit_price = "win", tgt
                        outcome_reason = "target_hit"
                        exit_time = str(ts); break
                # Expire if we walked past horizon without a hit.
                if (ts.tzinfo is None and ts >= horizon_end.replace(tzinfo=None)) or \
                   (ts.tzinfo is not None and ts >= horizon_end):
                    outcome = "expired"
                    exit_price = float(row.get("Close") or row.get("close"))
                    outcome_reason = "time_expired"
                    exit_time = str(ts); break

            if outcome is None:
                # Horizon not yet reached and no SL/Target hit — skip.
                continue

            pnl = (exit_price - entry) / entry * 100.0
            if r["action"] == "SELL":
                pnl = -pnl
            updates.append({
                "rec_id": r["rec_id"], "outcome": outcome,
                "exit_price": exit_price, "exit_time": exit_time,
                "pnl_pct": round(pnl, 2),
                "max_favorable_pct": round(max_fav, 2),
                "max_adverse_pct": round(max_adv, 2),
                "bars_held": bars_held,
                "outcome_reason": outcome_reason,
            })
            if outcome == "win": wins += 1
            elif outcome == "loss": losses += 1
            else: expired += 1

    if updates:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executemany(
                """UPDATE recommendation_outcomes
                   SET outcome=?, exit_price=?, exit_time=?, pnl_pct=?, evaluated_at=?,
                       max_favorable_pct=?, max_adverse_pct=?, bars_held=?, outcome_reason=?
                   WHERE rec_id=?""",
                [
                    (u["outcome"], u["exit_price"], u["exit_time"], u["pnl_pct"],
                     cutoff_now.isoformat(), u["max_favorable_pct"], u["max_adverse_pct"],
                     u["bars_held"], u["outcome_reason"], u["rec_id"])
                    for u in updates
                ],
            )
            await db.commit()
        await _recalculate_factor_performance()

    summary = {"evaluated": len(updates), "wins": wins, "losses": losses, "expired": expired}
    if updates:
        logger.info("Recommendation outcomes: %s", summary)
    return summary


# ── derive per-factor edge ──────────────────────────────────────────────

async def _recalculate_factor_performance() -> None:
    """Compute each factor's edge over the directional baseline.

    For each rec we split factors into two buckets:
      * "aligned" — factor's score was >0.3 in the recommendation's direction
                    (positive score for BUY, negative for SELL).
      * "neutral" — factor was close to zero or against the call.

    edge = avg pnl of recs where this factor was aligned
           − avg pnl of all directional recs.

    A positive edge means the factor was a leading indicator; negative
    means it was a contrarian signal we should down-weight.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT action, signals_json, pnl_pct
               FROM recommendation_outcomes
               WHERE outcome IN ('win','loss','expired')"""
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        return

    pnls_overall: list[float] = []
    aligned_pnls: dict[str, list[float]] = {}

    for row in rows:
        pnl = float(row["pnl_pct"]) if row["pnl_pct"] is not None else 0.0
        pnls_overall.append(pnl)
        try:
            sigs = json.loads(row["signals_json"] or "[]")
        except Exception:
            continue
        action = row["action"]
        for s in sigs:
            name = s.get("name")
            score = float(s.get("score") or 0.0)
            if not name:
                continue
            aligned = (action == "BUY" and score > 0.3) or (action == "SELL" and score < -0.3)
            if aligned:
                aligned_pnls.setdefault(name, []).append(pnl)

    overall_avg = sum(pnls_overall) / len(pnls_overall) if pnls_overall else 0.0
    now_iso = datetime.now(timezone.utc).isoformat()

    rows_to_write: list[tuple] = []
    new_cache: dict[str, float] = {}
    for factor, vals in aligned_pnls.items():
        aligned_avg = sum(vals) / len(vals)
        edge = aligned_avg - overall_avg
        rows_to_write.append((
            factor, len(pnls_overall), len(vals),
            round(aligned_avg, 4), round(overall_avg, 4), round(edge, 4), now_iso,
        ))
        new_cache[factor] = edge

    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """INSERT OR REPLACE INTO factor_performance
               (factor, total_directional, aligned_count, aligned_avg_pnl,
                overall_avg_pnl, edge, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows_to_write,
        )
        await db.commit()

    global _factor_edge_cache
    _factor_edge_cache = new_cache
    logger.info(
        "Factor performance updated: %d factors, dataset=%d directional recs",
        len(new_cache), len(pnls_overall),
    )


# ── consumed by recommendation._score_all ──────────────────────────────

def _edge_multiplier(edge: float) -> float:
    # Linear clip: edge of +5pp PnL -> 1.5x, -5pp -> 0.5x.
    mult = 1.0 + max(-0.5, min(0.5, edge / 10.0))
    return max(_WEIGHT_MIN, min(_WEIGHT_MAX, mult))


def factor_edge_multiplier(
    factor: str,
    *,
    regime: str | None = None,
    sector: str | None = None,
    horizon: str | None = None,
) -> float:
    """Map factor edge → weight multiplier in [0.5, 1.5].

    Edge is in percentage-points of P&L. Empirically, useful Indian-equity
    factor edges sit in the 1–4% range; saturating at ±5% gives a clean
    linear mapping into [0.5, 1.5]. Below `_MIN_SAMPLE_SIZE` directional
    recs we return 1.0 (no adjustment).
    """
    if not _factor_edge_cache:
        return 1.0
    candidates: list[str] = []
    if regime and horizon:
        candidates.append(f"{factor}|regime={regime}|horizon={horizon}")
    if sector and horizon:
        candidates.append(f"{factor}|sector={sector}|horizon={horizon}")
    if regime:
        candidates.append(f"{factor}|regime={regime}")
    if sector:
        candidates.append(f"{factor}|sector={sector}")
    if horizon:
        candidates.append(f"{factor}|horizon={horizon}")
    candidates.append(factor)

    multipliers = [
        _edge_multiplier(_factor_edge_cache[key])
        for key in candidates
        if key in _factor_edge_cache
    ]
    if not multipliers:
        return 1.0
    # Blend available context instead of allowing one narrow slice to dominate.
    return sum(multipliers) / len(multipliers)


async def seed_factor_edge_cache() -> int:
    """Load `factor_performance` into memory on startup.

    Mirrors `signal_tracker.seed_performance_cache` so dynamic weighting
    works immediately after a restart instead of only after the first cron
    tick.
    """
    global _factor_edge_cache
    cache: dict[str, float] = {}
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT factor, edge, total_directional FROM factor_performance"""
            ) as cur:
                async for row in cur:
                    if (row["total_directional"] or 0) >= _MIN_SAMPLE_SIZE:
                        cache[row["factor"]] = float(row["edge"] or 0.0)
    except Exception as e:
        logger.debug("seed_factor_edge_cache: %s", e)
    _factor_edge_cache = cache
    logger.info("Factor-edge cache seeded with %d factors on startup", len(cache))
    return len(cache)


def get_factor_edge_snapshot() -> dict[str, float]:
    """Read-only view of the current cache. Used by /api/performance/insights."""
    return dict(_factor_edge_cache)

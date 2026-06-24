"""De-duplicate paper_trades and recommendation_outcomes, then install the
open-position unique index.

Why this exists
---------------
The same logical paper trade used to be written more than once — once via the
live API/auto path and again via the legacy CSV import — because the only
dedup guard was scoped to ``source='auto'``. Separately, the recommendation
tracker keys rows on the exact ``generated_at`` timestamp, so the *same setup*
re-recommended on a later scan became a fresh row. Both inflate counts and
skew every win-rate / avg-PnL number the Perf tab reports (e.g. a 0-for-33
SELL run whose 33 includes duplicates).

What it does (idempotent, reversible)
-------------------------------------
1. Backs up both tables to ``*_dedup_backup`` (dropped + rebuilt each run).
2. paper_trades: within each (symbol, direction, entry_price, status) group,
   keeps the richest row and deletes the rest. "Richest" = has share count,
   then source priority (api > auto > module_a > csv_import), then newest.
3. recommendation_outcomes: within each
   (symbol, horizon, action, entry, stoploss, target1) group, keeps the most
   informative row (resolved outcome first, else earliest) and deletes dups.
4. Creates the partial UNIQUE index that prevents future open-position dups.

Run from the backend directory::

    cd backend
    python -m scripts.dedup_paper_data          # apply
    python -m scripts.dedup_paper_data --dry-run # report only, no writes
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

import aiosqlite

from app.database import DB_PATH, CREATE_PAPER_TRADES_DEDUP_INDEX

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("dedup_paper_data")

# Higher = preferred when picking which duplicate row to keep.
# auto (the real live engine) wins if it ever collides; then csv_import is
# preferred over api because, for the legacy api/csv duplicate pairs, the csv
# copy carries the realistic trailing/time exit while the api copy exited at a
# too-tight fixed -3% hard stop (the exit model we are moving away from) — that
# pessimistic copy was also the only one feeding the learning loop. Keeping the
# csv copy de-biases both the aggregate stats and the learner.
_SOURCE_RANK = {"auto": 4, "csv_import": 3, "api": 2, "module_a": 1}


def _paper_keep_score(row: dict[str, Any]) -> tuple:
    """Sort key — the row with the largest tuple is kept.

    Source rank is the PRIMARY key (ahead of has_shares): the legacy csv copies
    have NULL shares due to the old _int_or_none bug, so ranking has_shares first
    would always keep the api copy and defeat the csv-preferred policy above.
    has_shares stays as a secondary tiebreak within the same source.
    """
    source_rank = _SOURCE_RANK.get(row.get("source") or "", 0)
    has_shares = 1 if row.get("shares") is not None else 0
    created = row.get("created_at") or ""
    return (source_rank, has_shares, created)


def _reco_keep_score(row: dict[str, Any]) -> tuple:
    """Prefer a resolved row (real outcome), else the earliest created."""
    resolved = 1 if row.get("outcome") is not None else 0
    # Earliest created wins among same-resolution rows → negate via empty-last.
    created = row.get("created_at") or "9999"
    return (resolved, created == "", -_iso_ordinal(created))


def _iso_ordinal(s: str) -> float:
    """Cheap monotonic ordering of ISO strings without parsing."""
    return float(sum(ord(c) for c in s[:19]))


async def _dedup_table(
    db: aiosqlite.Connection,
    *,
    table: str,
    key_cols: list[str],
    id_col: str,
    keep_score,
    dry_run: bool,
) -> dict[str, int]:
    db.row_factory = aiosqlite.Row
    async with db.execute(f"SELECT * FROM {table}") as cur:
        rows = [dict(r) for r in await cur.fetchall()]

    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        key = tuple(r.get(c) for c in key_cols)
        groups.setdefault(key, []).append(r)

    delete_ids: list[Any] = []
    dup_groups = 0
    for key, members in groups.items():
        if len(members) <= 1:
            continue
        dup_groups += 1
        members.sort(key=keep_score, reverse=True)
        keeper = members[0]
        for loser in members[1:]:
            delete_ids.append(loser[id_col])
        logger.info(
            "  %s: %d dups for %s → keep %s, drop %d",
            table, len(members), key, keeper[id_col], len(members) - 1,
        )

    if delete_ids and not dry_run:
        await db.executemany(
            f"DELETE FROM {table} WHERE {id_col} = ?",
            [(i,) for i in delete_ids],
        )

    return {
        "rows": len(rows),
        "dup_groups": dup_groups,
        "deleted": len(delete_ids),
    }


async def main(dry_run: bool) -> None:
    logger.info("DB: %s%s", DB_PATH, "  (DRY RUN — no writes)" if dry_run else "")
    async with aiosqlite.connect(DB_PATH) as db:
        # 1. Backups (skip in dry-run).
        if not dry_run:
            for t in ("paper_trades", "recommendation_outcomes"):
                await db.execute(f"DROP TABLE IF EXISTS {t}_dedup_backup")
                await db.execute(f"CREATE TABLE {t}_dedup_backup AS SELECT * FROM {t}")
            logger.info("Backed up paper_trades + recommendation_outcomes to *_dedup_backup")

        # 2. paper_trades.
        logger.info("\npaper_trades:")
        pt = await _dedup_table(
            db, table="paper_trades",
            key_cols=["symbol", "direction", "entry_price", "status"],
            id_col="trade_id", keep_score=_paper_keep_score, dry_run=dry_run,
        )

        # 3. recommendation_outcomes.
        logger.info("\nrecommendation_outcomes:")
        ro = await _dedup_table(
            db, table="recommendation_outcomes",
            key_cols=["symbol", "horizon", "action", "entry", "stoploss", "target1"],
            id_col="rec_id", keep_score=_reco_keep_score, dry_run=dry_run,
        )

        # 4. Unique index (only after the table is clean).
        index_ok = False
        if not dry_run:
            await db.commit()
            try:
                await db.execute(CREATE_PAPER_TRADES_DEDUP_INDEX)
                await db.commit()
                index_ok = True
            except Exception as e:
                logger.error("Could not create open-dedup index: %s", e)

    logger.info(
        "\nSummary:\n  paper_trades: %d rows, %d dup groups, %d deleted"
        "\n  recommendation_outcomes: %d rows, %d dup groups, %d deleted"
        "\n  open-dedup index: %s",
        pt["rows"], pt["dup_groups"], pt["deleted"],
        ro["rows"], ro["dup_groups"], ro["deleted"],
        "created" if index_ok else ("dry-run" if dry_run else "FAILED"),
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report only, no writes")
    args = ap.parse_args()
    asyncio.run(main(args.dry_run))

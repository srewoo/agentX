"""Backfill llm_verdict / llm_reason for signals that pre-date the
2026-05-25 judge-token-budget fix.

Loads recent signals where ``llm_verdict IS NULL AND strength > 0``,
batches them through ``judge_signals`` exactly as a live scan would, and
writes the resulting verdicts back to the ``signals`` table.

Run from the backend directory::

    cd backend
    python -m scripts.backfill_llm_verdicts --since 2026-05-25 --batch 30
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from app.database import DB_PATH
from app.services.llm_signal_judge import judge_signals, is_enabled as judge_enabled
# Use the orchestrator's settings loader — it unseals encrypted API keys.
# Calling judge_signals with a sealed ciphertext as api_key would 401.
from app.services.orchestrator import _get_settings as _load_settings

logger = logging.getLogger(__name__)


async def _load_pending(since: str, limit: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, symbol, signal_type, direction, strength, reason,
                      current_price
               FROM signals
               WHERE llm_verdict IS NULL
                 AND strength > 0
                 AND dismissed = 0
                 AND created_at >= ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (since, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def _write_verdicts(verdicts_by_id: dict[str, Any]) -> int:
    if not verdicts_by_id:
        return 0
    async with aiosqlite.connect(DB_PATH) as db:
        n = 0
        for sig_id, v in verdicts_by_id.items():
            await db.execute(
                "UPDATE signals SET llm_verdict = ?, llm_reason = ? WHERE id = ?",
                (v.verdict, v.reason, sig_id),
            )
            n += 1
        await db.commit()
        return n


async def main(since: str, batch_size: int, max_total: int) -> None:
    settings = await _load_settings()
    if not judge_enabled(settings):
        print("LLM judging is disabled in settings (llm_judging_enabled=false). Aborting.")
        return

    total_done = 0
    while total_done < max_total:
        pending = await _load_pending(since, batch_size)
        if not pending:
            break

        print(f"Batch of {len(pending)} signals to judge...")
        verdicts = await judge_signals(pending, settings)
        wrote = await _write_verdicts(verdicts)
        total_done += wrote
        print(
            f"  → wrote {wrote} verdicts "
            f"(keep={sum(1 for v in verdicts.values() if v.verdict == 'keep')}, "
            f"downgrade={sum(1 for v in verdicts.values() if v.verdict == 'downgrade')}, "
            f"drop={sum(1 for v in verdicts.values() if v.verdict == 'drop')})"
        )

        # Mark any signals the LLM didn't comment on so the next iteration
        # doesn't re-pick them up. Treat silence as implicit 'keep' the way
        # the live judge does — keeps the loop bounded.
        commented = set(verdicts.keys())
        silent = [s["id"] for s in pending if s["id"] not in commented]
        if silent:
            async with aiosqlite.connect(DB_PATH) as db:
                for sid in silent:
                    await db.execute(
                        "UPDATE signals SET llm_verdict = 'keep', "
                        "llm_reason = 'implicit (judge silent)' WHERE id = ?",
                        (sid,),
                    )
                await db.commit()
            print(f"  → marked {len(silent)} silent signals as implicit keep")
            total_done += len(silent)

        if not verdicts and not silent:
            # No progress — bail to avoid an infinite loop.
            print("No progress this batch; stopping.")
            break

    print(f"Done. Total signals updated: {total_done}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser()
    default_since = (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).date().isoformat()
    parser.add_argument("--since", default=default_since,
                        help="ISO date; only signals created at/after this are touched")
    parser.add_argument("--batch", type=int, default=30, help="Signals per LLM call")
    parser.add_argument("--max", type=int, default=500, help="Safety cap on total updates")
    args = parser.parse_args()
    asyncio.run(main(args.since, args.batch, args.max))

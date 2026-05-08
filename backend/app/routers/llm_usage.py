from __future__ import annotations
"""LLM usage + spend reporting endpoints.

Exposes today/MTD token + cost totals plus a per-provider breakdown,
so the UI can show "you've used $X.XX / $CAP today".

Registration: this router is exported as ``llm_usage_router`` for the
main app to register. We deliberately do not edit ``main.py`` here —
the import path is documented in ``routers/__init__.py``.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter

from app.database import connect
from app.services.llm_client import _get_daily_cap_usd, _usd_inr

router = APIRouter(prefix="/api/llm", tags=["llm"])
# Public alias the main app should import + include_router on:
#     from app.routers.llm_usage import llm_usage_router
llm_usage_router = router

logger = logging.getLogger(__name__)


def _utc_day_start_iso() -> str:
    return datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()


def _utc_month_start_iso() -> str:
    return datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    ).isoformat()


async def _aggregate(conn, since_iso: str) -> dict:
    cursor = await conn.execute(
        """
        SELECT
          COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,
          COALESCE(SUM(cost_usd), 0)                          AS cost_usd,
          COALESCE(SUM(cost_inr), 0)                          AS cost_inr
        FROM llm_usage
        WHERE ts >= ?
        """,
        (since_iso,),
    )
    row = await cursor.fetchone()
    if not row:
        return {"tokens": 0, "costUsd": 0.0, "costInr": 0.0}
    return {
        "tokens": int(row[0] or 0),
        "costUsd": round(float(row[1] or 0.0), 6),
        "costInr": round(float(row[2] or 0.0), 4),
    }


async def _by_provider(conn, since_iso: str) -> list[dict]:
    cursor = await conn.execute(
        """
        SELECT
          provider,
          COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,
          COALESCE(SUM(cost_usd), 0)                          AS cost_usd
        FROM llm_usage
        WHERE ts >= ?
        GROUP BY provider
        ORDER BY cost_usd DESC
        LIMIT 50
        """,
        (since_iso,),
    )
    rows = await cursor.fetchall()
    return [
        {
            "provider": r[0],
            "tokens": int(r[1] or 0),
            "costUsd": round(float(r[2] or 0.0), 6),
        }
        for r in rows
    ]


@router.get("/usage")
async def get_llm_usage() -> dict:
    """Return today + month-to-date LLM usage totals and the daily USD cap."""
    day_start = _utc_day_start_iso()
    month_start = _utc_month_start_iso()

    cap_usd = await _get_daily_cap_usd()

    async with connect() as conn:
        today = await _aggregate(conn, day_start)
        mtd = await _aggregate(conn, month_start)
        by_provider = await _by_provider(conn, month_start)

    cap_remaining = max(cap_usd - today["costUsd"], 0.0) if cap_usd > 0 else 0.0

    return {
        "today": today,
        "mtd": mtd,
        "capUsd": round(cap_usd, 4),
        "capRemainingUsd": round(cap_remaining, 6),
        "byProvider": by_provider,
        "usdInrRate": _usd_inr(),
    }

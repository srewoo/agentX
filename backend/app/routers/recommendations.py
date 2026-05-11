from __future__ import annotations
"""Recommendation HTTP endpoints.

Wiring (parent agent should add to main.py):
    from app.routers import recommendations
    app.include_router(recommendations.router)

Endpoints:
    GET /api/recommendations            - batch, filterable
    GET /api/recommendations/{symbol}   - single symbol
    GET /api/recommendations/sectors    - sector summaries
"""
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query

from app.models.recommendation import (
    Horizon,
    Recommendation,
    RecommendationListResponse,
    SectorListResponse,
    SectorSummary,
)

# Accept the frontend's legacy "long" alias and normalize to the canonical
# "positional" value before any downstream code sees it.
HorizonQuery = Literal["intraday", "swing", "positional", "long"]


def _normalize_horizon(h: HorizonQuery) -> Horizon:
    return "positional" if h == "long" else h  # type: ignore[return-value]
from app.services.recommendation import (
    default_universe,
    generate_batch,
    generate_recommendation,
)

router = APIRouter(prefix="/api/recommendations", tags=["recommendations"])
logger = logging.getLogger(__name__)

_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20
# How many symbols we actually run the engine over. Each run costs an NSE
# fetch + yfinance fallback + RS calc; 100 cold = 60s+ on first request.
# 40 covers the common UI usage (top-N filter <= 40) and the orchestrator's
# background pass keeps the cache warm for deeper queries.
_UNIVERSE_PER_REQUEST = 40


@router.get("", response_model=RecommendationListResponse)
async def list_recommendations(
    horizon: HorizonQuery = "swing",
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    sector: Optional[str] = None,
    min_conviction: int = Query(0, ge=0, le=100),
) -> RecommendationListResponse:
    """Generate recommendations across the default Indian universe.

    Filters apply *after* generation so cached entries stay re-usable.
    """
    horizon = _normalize_horizon(horizon)
    universe = default_universe(limit=_UNIVERSE_PER_REQUEST)
    started = datetime.now(timezone.utc)
    recs, errors = await generate_batch(universe, horizon=horizon)

    # Filter then sort (highest conviction first), then truncate.
    filtered = [
        r for r in recs
        if r.conviction >= min_conviction
        and (not sector or r.sector.lower() == sector.lower())
    ]
    filtered.sort(key=lambda r: (r.conviction, r.risk_reward), reverse=True)
    truncated = filtered[:limit]

    meta = {
        "horizon": horizon,
        "universe_size": len(universe),
        "generated": len(recs),
        "returned": len(truncated),
        "filtered_out": len(recs) - len(filtered),
        "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
        "filters": {"sector": sector, "min_conviction": min_conviction},
    }
    return RecommendationListResponse(data=truncated, meta=meta, errors=errors)


@router.get("/sectors", response_model=SectorListResponse)
async def list_sector_summaries(
    horizon: HorizonQuery = "swing",
) -> SectorListResponse:
    """Per-sector average conviction and top picks (top 3 by conviction)."""
    horizon = _normalize_horizon(horizon)
    universe = default_universe(limit=_UNIVERSE_PER_REQUEST)
    recs, errors = await generate_batch(universe, horizon=horizon)

    by_sector: dict[str, list[Recommendation]] = defaultdict(list)
    for r in recs:
        if r.action != "AVOID":
            by_sector[r.sector].append(r)

    summaries: list[SectorSummary] = []
    for sec, items in by_sector.items():
        items.sort(key=lambda r: r.conviction, reverse=True)
        avg = sum(r.conviction for r in items) / len(items)
        summaries.append(
            SectorSummary(
                sector=sec,
                avg_conviction=round(avg, 1),
                pick_count=len(items),
                top_picks=[r.symbol for r in items[:3]],
            )
        )
    summaries.sort(key=lambda s: s.avg_conviction, reverse=True)

    return SectorListResponse(
        data=summaries,
        meta={"horizon": horizon, "sector_count": len(summaries)},
        errors=errors,
    )


@router.get("/{symbol}", response_model=dict)
async def get_recommendation(
    symbol: str,
    horizon: HorizonQuery = "swing",
    llm_judge: bool = False,
    reasoning_effort: Literal["low", "medium", "high"] = "medium",
):
    """Single-symbol recommendation. 404 when no data is available."""
    horizon = _normalize_horizon(horizon)
    sym = symbol.strip().upper()
    if not sym or len(sym) > 25:
        raise HTTPException(status_code=400, detail="Invalid symbol")
    rec = await generate_recommendation(
        sym,
        horizon=horizon,
        use_llm_judge=llm_judge,
        reasoning_effort=reasoning_effort,
    )
    if rec is None:
        raise HTTPException(
            status_code=404,
            detail=f"No recommendation available for {sym} (insufficient data).",
        )
    return {"data": rec, "meta": {"horizon": horizon}, "errors": []}

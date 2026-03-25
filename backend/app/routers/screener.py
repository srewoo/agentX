from __future__ import annotations
"""Screener endpoints — TradingView-powered stock screening for the UI."""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException

from app.services.screener import (
    SCREENER_PRESETS,
    run_screener_query,
)

router = APIRouter(prefix="/api/screener", tags=["screener"])
logger = logging.getLogger(__name__)


@router.get("")
async def screener(
    rsi_min: Optional[float] = None,
    rsi_max: Optional[float] = None,
    volume_ratio_min: Optional[float] = None,
    change_pct_min: Optional[float] = None,
    change_pct_max: Optional[float] = None,
    market_cap_min: Optional[float] = None,
    market_cap_max: Optional[float] = None,
    sector: Optional[str] = None,
    limit: int = 50,
):
    """
    Run a stock screener with TradingView data.

    Query params:
      - rsi_min / rsi_max: RSI range filter
      - volume_ratio_min: minimum current_vol / avg_vol ratio
      - change_pct_min / change_pct_max: % change filter
      - market_cap_min / market_cap_max: market cap filter (in absolute value, e.g. 1e11 = 10K Cr)
      - sector: sector name filter
      - limit: max results (default 50, max 200)
    """
    query_params: dict = {}
    if rsi_min is not None:
        query_params["rsi_min"] = rsi_min
    if rsi_max is not None:
        query_params["rsi_max"] = rsi_max
    if volume_ratio_min is not None:
        query_params["volume_ratio_min"] = volume_ratio_min
    if change_pct_min is not None:
        query_params["change_pct_min"] = change_pct_min
    if change_pct_max is not None:
        query_params["change_pct_max"] = change_pct_max
    if market_cap_min is not None:
        query_params["market_cap_min"] = market_cap_min
    if market_cap_max is not None:
        query_params["market_cap_max"] = market_cap_max
    if sector is not None:
        query_params["sector"] = sector
    query_params["limit"] = limit

    # If no filters at all, require at least one
    filter_keys = {"rsi_min", "rsi_max", "volume_ratio_min", "change_pct_min",
                   "change_pct_max", "market_cap_min", "market_cap_max", "sector"}
    if not any(k in query_params for k in filter_keys):
        raise HTTPException(
            status_code=400,
            detail="At least one filter parameter is required. Use /api/screener/presets to see available presets.",
        )

    try:
        results = run_screener_query(query_params)
        return {"count": len(results), "results": results}
    except Exception as e:
        logger.error(f"Screener endpoint error: {e}")
        raise HTTPException(status_code=500, detail="Screener query failed. TradingView may be unavailable.")


@router.get("/presets")
async def screener_presets():
    """
    Returns preset screener configurations.
    Each preset has a label, description, and params that can be passed to GET /api/screener.
    """
    return {"presets": SCREENER_PRESETS}


@router.get("/presets/{preset_name}")
async def run_preset(preset_name: str, limit: int = 50):
    """Run a named preset screener (oversold, overbought, volume_breakout, momentum, etc.)."""
    preset = SCREENER_PRESETS.get(preset_name)
    if not preset:
        available = list(SCREENER_PRESETS.keys())
        raise HTTPException(
            status_code=404,
            detail=f"Preset '{preset_name}' not found. Available: {available}",
        )

    params = {**preset["params"], "limit": limit}
    try:
        results = run_screener_query(params)
        return {
            "preset": preset_name,
            "label": preset["label"],
            "description": preset["description"],
            "count": len(results),
            "results": results,
        }
    except Exception as e:
        logger.error(f"Preset screener error for '{preset_name}': {e}")
        raise HTTPException(status_code=500, detail="Screener query failed. TradingView may be unavailable.")

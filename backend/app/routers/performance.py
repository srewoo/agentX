from __future__ import annotations
"""Performance tracking endpoints — win/loss stats for signal outcomes."""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException

from app.services.signal_tracker import (
    evaluate_signals,
    get_performance_stats,
    get_performance_summary,
    get_signal_accuracy,
)

router = APIRouter(prefix="/api/performance", tags=["performance"])
logger = logging.getLogger(__name__)


@router.get("/summary")
async def performance_summary():
    """Return overall win rate, avg PnL, and total signals evaluated."""
    try:
        summary = await get_performance_summary()
        return {"data": summary}
    except Exception as e:
        logger.error(f"Error fetching performance summary: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch performance summary")


@router.get("/by-type")
async def performance_by_type(
    signal_type: Optional[str] = None,
    direction: Optional[str] = None,
):
    """
    Return performance breakdown by signal_type.
    Optionally filter by signal_type and/or direction.
    """
    try:
        if signal_type and direction:
            accuracy = await get_signal_accuracy(signal_type, direction)
            return {"data": [accuracy]}

        stats = await get_performance_stats()

        # Apply optional filters
        if signal_type:
            stats = [s for s in stats if s["signal_type"] == signal_type]
        if direction:
            stats = [s for s in stats if s["direction"] == direction]

        return {"data": stats}
    except Exception as e:
        logger.error(f"Error fetching performance by type: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch performance stats")


@router.post("/evaluate")
async def trigger_evaluation():
    """Manually trigger evaluation of signal outcomes."""
    try:
        result = await evaluate_signals()
        return {"data": result, "message": "Signal evaluation complete"}
    except Exception as e:
        logger.error(f"Error during manual evaluation: {e}")
        raise HTTPException(status_code=500, detail="Failed to evaluate signals")

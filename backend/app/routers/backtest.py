from __future__ import annotations
"""Backtest endpoints — run historical backtests on the signal engine."""
import logging
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException

from app.services.backtester import run_backtest
from app.services.cache import cache_manager, make_cache_key

router = APIRouter(prefix="/api/backtest", tags=["backtest"])
logger = logging.getLogger(__name__)

CACHE_TTL = timedelta(hours=24)


@router.post("/{symbol}")
async def backtest_symbol(
    symbol: str,
    period: Optional[str] = "1y",
    eval_days: Optional[int] = 5,
):
    """
    Run a historical backtest of the signal engine on a symbol.

    - **symbol**: Stock symbol (e.g. RELIANCE, TCS, INFY)
    - **period**: yfinance period string (6mo, 1y, 2y). Default: 1y.
    - **eval_days**: Primary evaluation window in days. Default: 5.
      All standard windows (1, 3, 5, 10) are always computed.
    """
    symbol = symbol.upper().strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required.")

    allowed_periods = {"3mo", "6mo", "1y", "2y", "5y"}
    if period not in allowed_periods:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid period. Allowed: {', '.join(sorted(allowed_periods))}",
        )

    # Check cache
    cache_key = make_cache_key("backtest", symbol, period, eval_days=eval_days)
    cached = await cache_manager.get(cache_key)
    if cached:
        logger.info("Backtest cache hit: %s", cache_key)
        return cached

    logger.info("Running backtest: symbol=%s period=%s eval_days=%d", symbol, period, eval_days)

    eval_windows = sorted(set([1, 3, 5, 10, eval_days]))

    try:
        result = await run_backtest(
            symbol=symbol,
            period=period,
            eval_windows=eval_windows,
        )
    except Exception as exc:
        logger.exception("Backtest failed for %s: %s", symbol, exc)
        raise HTTPException(status_code=500, detail=f"Backtest failed: {str(exc)}")

    if result.get("error"):
        raise HTTPException(status_code=422, detail=result["error"])

    # Cache for 24 hours
    await cache_manager.set(cache_key, result, ttl=CACHE_TTL)

    return result

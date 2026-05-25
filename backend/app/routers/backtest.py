from __future__ import annotations
"""Backtest endpoints — run historical backtests on the signal engine."""
import logging
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException

from app.services.backtester import run_backtest
from app.services.backtester_walk_forward import (
    run_universe_walk_forward,
    run_walk_forward,
)
from app.services.cache import cache_manager, make_cache_key

router = APIRouter(prefix="/api/backtest", tags=["backtest"])
logger = logging.getLogger(__name__)

CACHE_TTL = timedelta(hours=24)


@router.post("/{symbol}")
async def backtest_symbol(
    symbol: str,
    period: Optional[str] = "1y",
    eval_days: Optional[int] = 5,
    exchange: str = "NSE",
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

    ex = (exchange or "NSE").upper()
    if ex not in {"NSE", "BSE"}:
        ex = "NSE"

    # Check cache
    cache_key = make_cache_key("backtest", symbol, ex, period, eval_days=eval_days)
    cached = await cache_manager.get(cache_key)
    if cached:
        logger.info("Backtest cache hit: %s", cache_key)
        return cached

    logger.info("Running backtest: symbol=%s exchange=%s period=%s eval_days=%d", symbol, ex, period, eval_days)

    eval_windows = sorted(set([1, 3, 5, 10, eval_days]))

    try:
        result = await run_backtest(
            symbol=symbol,
            period=period,
            eval_windows=eval_windows,
            exchange=ex,
        )
    except Exception as exc:
        logger.exception("Backtest failed for %s: %s", symbol, exc)
        raise HTTPException(status_code=500, detail=f"Backtest failed: {str(exc)}")

    if result.get("error"):
        raise HTTPException(status_code=422, detail=result["error"])

    # Cache for 24 hours
    await cache_manager.set(cache_key, result, ttl=CACHE_TTL)

    return result


@router.post("/walk-forward/{symbol}")
async def walk_forward_symbol(
    symbol: str,
    period: str = "2y",
    n_folds: int = 4,
    exchange: str = "NSE",
):
    """Walk-forward (out-of-sample) backtest. Expanding-window K-fold split.

    Each fold trains stats on bars [0..train_end] and tests on the next
    chunk — the engine never sees future data. Returns per-fold metrics
    plus a pooled OOS summary with Wilson 95% lower bounds so small-n
    win rates don't lie.
    """
    symbol = symbol.upper().strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required.")
    if n_folds < 2 or n_folds > 10:
        raise HTTPException(status_code=400, detail="n_folds must be 2..10")
    key = make_cache_key("wf", symbol, exchange, period, folds=n_folds)
    cached = await cache_manager.get(key)
    if cached:
        return cached
    result = await run_walk_forward(symbol, period=period, n_folds=n_folds, exchange=exchange)
    if result.get("error"):
        raise HTTPException(status_code=422, detail=result["error"])
    await cache_manager.set(key, result, ttl=CACHE_TTL)
    return result


@router.post("/walk-forward")
async def walk_forward_universe(
    period: str = "2y",
    n_folds: int = 4,
    limit: int = 40,
):
    """Universe-wide walk-forward over the top-N NSE majors.

    Pools every OOS trade across symbols and folds so per-signal-type
    win rates are based on the largest possible honest sample.
    """
    if n_folds < 2 or n_folds > 10:
        raise HTTPException(status_code=400, detail="n_folds must be 2..10")
    if limit < 5 or limit > 100:
        raise HTTPException(status_code=400, detail="limit must be 5..100")
    key = make_cache_key("wf-universe", period, folds=n_folds, limit=limit)
    cached = await cache_manager.get(key)
    if cached:
        return cached
    from app.services.data_fetcher import MAJOR_STOCKS
    symbols = [s["symbol"] for s in MAJOR_STOCKS if not s["symbol"].startswith("^")][:limit]
    result = await run_universe_walk_forward(symbols=symbols, period=period, n_folds=n_folds)
    await cache_manager.set(key, result, ttl=CACHE_TTL)
    return result

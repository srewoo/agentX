"""AI analysis endpoint (user-triggered, on-demand)."""
import logging
from datetime import timedelta

from fastapi import APIRouter, HTTPException

from app.models import AIAnalysisRequest
from app.services.data_fetcher import async_fetch_history, get_stock_info
from app.services.technicals import (
    compute_fibonacci_levels,
    compute_support_resistance,
    compute_technicals,
    compute_volume_profile_poc,
)
from app.services.llm_analyst import run_analysis
from app.services.cache import cache_manager, make_cache_key
from app.database import DB_PATH
from app.utils import sanitize_symbol
import aiosqlite

router = APIRouter(prefix="/api/stocks", tags=["analysis"])
logger = logging.getLogger(__name__)


async def _get_settings() -> dict:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT key, value FROM settings") as cursor:
                rows = await cursor.fetchall()
                return {row["key"]: row["value"] for row in rows}
    except Exception:
        return {}


@router.post("/{symbol}/ai-analysis")
async def get_ai_analysis(symbol: str, body: AIAnalysisRequest):
    """Run AI analysis for a stock. User-triggered, on-demand."""
    symbol = sanitize_symbol(symbol)
    cache_key = make_cache_key("stock:analysis", symbol, timeframe=body.timeframe)

    cached = await cache_manager.get(cache_key)
    if cached:
        return cached

    df = await async_fetch_history(symbol, period="1y", interval="1d")
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"No data found for {symbol}")

    technicals = compute_technicals(df)
    if not technicals:
        raise HTTPException(status_code=422, detail="Insufficient data for analysis")

    sr = compute_support_resistance(df)
    fib = compute_fibonacci_levels(df)
    poc = compute_volume_profile_poc(df)
    stock_info = get_stock_info(symbol)
    settings = await _get_settings()

    analysis = await run_analysis(
        symbol=symbol,
        timeframe=body.timeframe,
        technicals=technicals,
        sr=sr,
        fib=fib,
        poc=poc,
        stock_info=stock_info,
        settings=settings,
    )

    result = {
        "symbol": symbol,
        "name": stock_info.get("name", symbol),
        "timeframe": body.timeframe,
        "current_price": technicals.get("current_price"),
        "analysis": analysis,
        "support_resistance": sr,
        "fibonacci": fib,
        "poc": poc,
    }

    await cache_manager.set(cache_key, result, ttl=timedelta(minutes=30))
    return result

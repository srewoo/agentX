"""Market-level endpoints — indices, news, scan trigger, health."""
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite
from fastapi import APIRouter

from app.database import DB_PATH
from app.services.data_fetcher import async_fetch_history
from app.services.sentiment import get_market_news, get_sentiment_summary
from app.services.orchestrator import orchestrator, run_scan_cycle, last_scan_time, is_market_open
from app.services.cache import cache_manager, make_cache_key
from app.utils import safe_float

router = APIRouter(prefix="/api", tags=["market"])
logger = logging.getLogger(__name__)

INDICES = [
    {"symbol": "^NSEI", "name": "NIFTY 50"},
    {"symbol": "^BSESN", "name": "BSE SENSEX"},
]


@router.get("/health")
async def health_check():
    """Backend health check."""
    db_ok = "ok"
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("SELECT 1")
    except Exception:
        db_ok = "error"

    return {
        "status": "ok",
        "db": db_ok,
        "cache": "ok" if cache_manager.enabled else "disabled",
        "last_scan": last_scan_time,
        "market_open": is_market_open(),
        "orchestrator_running": orchestrator.is_running(),
    }


@router.get("/market/indices")
async def get_indices():
    """Get NIFTY 50 and SENSEX live quotes."""
    cache_key = "market:indices"
    cached = await cache_manager.get(cache_key)
    if cached:
        return cached

    result = {}
    for idx in INDICES:
        try:
            df = await async_fetch_history(idx["symbol"], period="5d", interval="1d")
            if df is not None and not df.empty:
                current = safe_float(df["Close"].iloc[-1])
                prev = safe_float(df["Close"].iloc[-2]) if len(df) > 1 else None
                change = round(current - prev, 2) if current and prev else None
                change_pct = round((change / prev) * 100, 2) if change and prev else None
                result[idx["name"]] = {
                    "symbol": idx["symbol"],
                    "price": current,
                    "change": change,
                    "change_pct": change_pct,
                }
        except Exception as e:
            logger.warning(f"Failed to fetch {idx['symbol']}: {e}")

    await cache_manager.set(cache_key, result, ttl=timedelta(minutes=5))
    return result


@router.get("/market/news")
async def get_news(limit: int = 20):
    """Fetch market news with sentiment scores."""
    cache_key = make_cache_key("market:news", limit=limit)
    cached = await cache_manager.get(cache_key)
    if cached:
        return cached

    try:
        news = await get_market_news(limit=limit)
        result = {"news": news, "count": len(news)}
        await cache_manager.set(cache_key, result, ttl=timedelta(minutes=15))
        return result
    except Exception as e:
        logger.error(f"News fetch error: {e}")
        return {"news": [], "count": 0}


@router.post("/scan/trigger")
async def trigger_scan():
    """Manually trigger a scan cycle. Returns signals found."""
    import time
    start = time.time()
    try:
        signals = await run_scan_cycle()
        elapsed_ms = int((time.time() - start) * 1000)
        return {
            "signals_found": len(signals),
            "scan_duration_ms": elapsed_ms,
            "signals": signals[:10],  # Return first 10 for preview
        }
    except Exception as e:
        logger.error(f"Manual scan failed: {e}")
        return {"signals_found": 0, "scan_duration_ms": 0, "error": str(e)}

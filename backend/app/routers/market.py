"""Market-level endpoints — indices, news, scan trigger, health."""
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite
from fastapi import APIRouter

from app.database import DB_PATH
from app.services.data_fetcher import async_fetch_history
from app.services.nse_fetcher import nse_fetch_indices, nse_market_status
from app.services.sentiment import get_market_news, get_sentiment_summary
from app.services.orchestrator import orchestrator, run_scan_cycle, last_scan_time, is_market_open
from app.services.cache import cache_manager, make_cache_key
from app.utils import safe_float

router = APIRouter(prefix="/api", tags=["market"])
logger = logging.getLogger(__name__)


@router.get("/health")
async def health_check():
    """Backend health check with real NSE market status."""
    db_ok = "ok"
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("SELECT 1")
    except Exception:
        db_ok = "error"

    # Try to get real market status from NSE
    market_open = is_market_open()  # fallback: IST time check
    nse_status_msg = None
    try:
        statuses = await nse_market_status()
        if statuses:
            for s in statuses:
                if s.get("market") == "Capital Market":
                    ms = s.get("marketStatus", "")
                    market_open = ms in ("Open", "Pre Open")
                    nse_status_msg = s.get("marketStatusMessage")
                    break
    except Exception:
        pass

    return {
        "status": "ok",
        "db": db_ok,
        "cache": "ok" if cache_manager.enabled else "disabled",
        "last_scan": last_scan_time,
        "market_open": market_open,
        "market_status_message": nse_status_msg,
        "orchestrator_running": orchestrator.is_running(),
    }


@router.get("/market/indices")
async def get_indices():
    """Get NIFTY 50, NIFTY BANK, INDIA VIX from NSE (fast, no yfinance)."""
    cache_key = "market:indices"
    cached = await cache_manager.get(cache_key)
    if cached:
        return cached

    result = {}

    # Try NSE first (returns NIFTY 50, NIFTY BANK, INDIA VIX from status endpoint)
    try:
        nse_data = await nse_fetch_indices()
        if nse_data:
            for name, data in nse_data.items():
                result[name] = {
                    "symbol": name,
                    "price": safe_float(data.get("last")),
                    "change": safe_float(data.get("variation")),
                    "change_pct": safe_float(data.get("percentChange")),
                    "market_status": data.get("marketStatus"),
                }
    except Exception as e:
        logger.debug("NSE indices failed: %s", e)

    # Fallback to yfinance if NSE returned nothing
    if not result:
        for sym, name in [("^NSEI", "NIFTY 50"), ("^BSESN", "BSE SENSEX")]:
            try:
                df = await async_fetch_history(sym, period="5d", interval="1d")
                if df is not None and not df.empty:
                    current = safe_float(df["Close"].iloc[-1])
                    prev = safe_float(df["Close"].iloc[-2]) if len(df) > 1 else None
                    change = round(current - prev, 2) if current and prev else None
                    change_pct = round((change / prev) * 100, 2) if change and prev else None
                    result[name] = {
                        "symbol": sym,
                        "price": current,
                        "change": change,
                        "change_pct": change_pct,
                    }
            except Exception as e:
                logger.warning("Failed to fetch %s: %s", sym, e)

    if result:
        await cache_manager.set(cache_key, result, ttl=timedelta(minutes=2))
    return result


@router.get("/market/actions")
async def get_actions():
    """Upcoming corporate actions (dividends, splits, bonuses) from NSE."""
    cache_key = "market:actions"
    cached = await cache_manager.get(cache_key)
    if cached:
        return cached

    try:
        from app.services.market_data import get_corporate_actions
        actions = await get_corporate_actions()
        result = {"actions": actions, "count": len(actions)}
        await cache_manager.set(cache_key, result, ttl=timedelta(minutes=30))
        return result
    except Exception as e:
        logger.error("Actions fetch error: %s", e)
        return {"actions": [], "count": 0}


@router.get("/market/block-deals")
async def get_block_deals_endpoint():
    """Today's block deals (institutional transactions) from NSE."""
    cache_key = "market:block_deals"
    cached = await cache_manager.get(cache_key)
    if cached:
        return cached

    try:
        from app.services.market_data import get_block_deals
        deals = await get_block_deals()
        result = {"deals": deals, "count": len(deals)}
        await cache_manager.set(cache_key, result, ttl=timedelta(minutes=5))
        return result
    except Exception as e:
        logger.error("Block deals fetch error: %s", e)
        return {"deals": [], "count": 0}


@router.get("/market/options/{symbol}")
async def get_options_analysis(symbol: str):
    """Options chain analysis (PCR, max pain, unusual OI) from NSE."""
    from app.utils import sanitize_symbol
    symbol = sanitize_symbol(symbol)
    cache_key = make_cache_key("market:options", symbol)
    cached = await cache_manager.get(cache_key)
    if cached:
        return cached

    try:
        from app.services.market_data import get_option_chain_analysis
        analysis = await get_option_chain_analysis(symbol)
        if not analysis:
            return {"error": f"No options data for {symbol}. May not be FnO-eligible."}
        await cache_manager.set(cache_key, analysis, ttl=timedelta(minutes=5))
        return analysis
    except Exception as e:
        logger.error("Options analysis error for %s: %s", symbol, e)
        return {"error": str(e)}


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


@router.get("/market/context")
async def get_market_context_summary():
    """Market context summary: FII/DII flows, India VIX, NIFTY regime. Used by the frontend dashboard."""
    from app.services.fii_dii import get_fii_dii_data
    from app.services.market_data import get_india_vix
    from app.services.market_regime import detect_market_regime

    cache_key = "market:context"
    cached = await cache_manager.get(cache_key)
    if cached:
        return cached

    result: dict = {
        "fii_dii": None,
        "india_vix": None,
        "market_regime": None,
    }

    # FII/DII flows
    try:
        fii_data = await get_fii_dii_data()
        result["fii_dii"] = fii_data
    except Exception as e:
        logger.debug("FII/DII fetch failed for context: %s", e)

    # India VIX
    try:
        vix = await get_india_vix()
        result["india_vix"] = vix
    except Exception as e:
        logger.debug("India VIX fetch failed for context: %s", e)

    # Market regime (NIFTY 50)
    try:
        nifty_df = await async_fetch_history("^NSEI", period="1y", interval="1d")
        if nifty_df is not None and not nifty_df.empty and len(nifty_df) >= 200:
            regime = detect_market_regime(nifty_df)
            result["market_regime"] = regime
    except Exception as e:
        logger.debug("Market regime detection failed: %s", e)

    if any(v is not None for v in result.values()):
        await cache_manager.set(cache_key, result, ttl=timedelta(minutes=10))
    return result


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

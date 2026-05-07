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
    """Get NIFTY 50, NIFTY BANK, INDIA VIX (NSE) + BSE SENSEX (yfinance)."""
    import asyncio

    cache_key = "market:indices"
    cached = await cache_manager.get(cache_key)
    if cached:
        return cached

    result: dict = {}

    async def _from_yf(symbol: str, name: str):
        """Fetch one index from yfinance and shape it like the NSE rows."""
        try:
            df = await async_fetch_history(symbol, period="5d", interval="1d")
            if df is None or df.empty:
                return None
            current = safe_float(df["Close"].iloc[-1])
            prev = safe_float(df["Close"].iloc[-2]) if len(df) > 1 else None
            change = round(current - prev, 2) if current and prev else None
            change_pct = round((change / prev) * 100, 2) if change and prev else None
            return name, {
                "symbol": symbol,
                "price": current,
                "change": change,
                "change_pct": change_pct,
            }
        except Exception as e:
            logger.warning("Failed to fetch %s: %s", symbol, e)
            return None

    # NSE indices and BSE SENSEX in parallel — SENSEX always comes from yfinance
    # because NSE's status endpoint doesn't carry it.
    async def _nse():
        try:
            return await nse_fetch_indices()
        except Exception as e:
            logger.debug("NSE indices failed: %s", e)
            return None

    nse_data, sensex = await asyncio.gather(_nse(), _from_yf("^BSESN", "BSE SENSEX"))

    if nse_data:
        for name, data in nse_data.items():
            result[name] = {
                "symbol": name,
                "price": safe_float(data.get("last")),
                "change": safe_float(data.get("variation")),
                "change_pct": safe_float(data.get("percentChange")),
                "market_status": data.get("marketStatus"),
            }

    if sensex:
        sensex_name, sensex_payload = sensex
        result[sensex_name] = sensex_payload

    # Fallback: NSE failed AND SENSEX yfinance failed → try NIFTY via yfinance
    if not result:
        nifty = await _from_yf("^NSEI", "NIFTY 50")
        if nifty:
            n_name, n_payload = nifty
            result[n_name] = n_payload

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
        # Normalize wire shape to match the frontend NewsItem contract.
        # Internally we use {link, published, sentiment_score, relevance_symbols};
        # the extension reads {url, published_at, sentiment, symbols}.
        normalized = [
            {
                "title": n.get("title"),
                "url": n.get("link") or n.get("url"),
                "source": n.get("source"),
                "published_at": n.get("published") or n.get("published_at"),
                "sentiment": n.get("sentiment_score") if n.get("sentiment_score") is not None else n.get("sentiment"),
                "symbols": n.get("relevance_symbols") or n.get("symbols") or [],
                "summary": n.get("summary"),
            }
            for n in news
        ]
        result = {"news": normalized, "count": len(normalized)}
        await cache_manager.set(cache_key, result, ttl=timedelta(minutes=15))
        return result
    except Exception as e:
        logger.error(f"News fetch error: {e}")
        return {"news": [], "count": 0}


@router.get("/market/context")
async def get_market_context_summary():
    """Market context summary: FII/DII flows, India VIX, NIFTY regime. Used by the frontend dashboard."""
    import asyncio
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

    # Run all fetches in parallel with individual timeouts
    async def _fii():
        return await asyncio.wait_for(get_fii_dii_data(), timeout=15)

    async def _vix():
        return await asyncio.wait_for(get_india_vix(), timeout=15)

    async def _regime():
        nifty_df = await asyncio.wait_for(
            async_fetch_history("^NSEI", period="1y", interval="1d"), timeout=30
        )
        if nifty_df is not None and not nifty_df.empty and len(nifty_df) >= 200:
            return detect_market_regime(nifty_df)
        return None

    results = await asyncio.gather(_fii(), _vix(), _regime(), return_exceptions=True)

    if not isinstance(results[0], BaseException):
        result["fii_dii"] = results[0]
    if not isinstance(results[1], BaseException) and results[1] is not None:
        result["india_vix"] = results[1]
    if not isinstance(results[2], BaseException) and results[2] is not None:
        result["market_regime"] = results[2]

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

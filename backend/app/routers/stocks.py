"""Stock data endpoints — search, quote, history, technicals."""
import asyncio
import logging
from datetime import timedelta
from typing import Optional

import yfinance as yf
from fastapi import APIRouter, HTTPException

from app.services.data_fetcher import MAJOR_STOCKS, async_fetch_history, get_stock_info, resilient_fetch_history
from app.services.nse_fetcher import nse_fetch_quote
from app.services.screener import get_all_indian_stocks
from app.services.technicals import (
    compute_fibonacci_levels,
    compute_support_resistance,
    compute_technicals,
    compute_volume_profile_poc,
)
from app.services.market_regime import detect_market_regime
from app.services.cache import cache_manager, make_cache_key
from app.utils import safe_float, sanitize_symbol

router = APIRouter(prefix="/api/stocks", tags=["stocks"])
logger = logging.getLogger(__name__)

# Build a simple search index from MAJOR_STOCKS
_SEARCH_INDEX = [
    {"symbol": s["symbol"], "name": s["name"], "exchange": "NSE", "sector": s.get("sector", "")}
    for s in MAJOR_STOCKS
]


@router.get("/search")
async def search_stocks(q: str = ""):
    """Search stocks by symbol or name. Falls back to yfinance for unknown symbols."""
    if not q or len(q) < 1:
        return {"results": _SEARCH_INDEX[:20]}

    q_upper = q.upper()
    q_lower = q.lower()
    results = [
        s for s in _SEARCH_INDEX
        if q_upper in s["symbol"] or q_lower in s["name"].lower()
    ]

    # If no index match, search the cached TradingView stock list (2000+ stocks)
    if not results and len(q) >= 2:
        try:
            tv_stocks = get_all_indian_stocks()
            results = [
                {"symbol": s["symbol"], "name": s["name"], "exchange": s.get("exchange", "NSE"), "sector": s.get("sector", "")}
                for s in tv_stocks
                if q_upper in s["symbol"].upper() or q_lower in s.get("name", "").lower()
            ]
        except Exception as e:
            logger.warning(f"TradingView stock search fallback failed: {e}")

    # If still no match, try direct yfinance lookup so any NSE/BSE symbol works
    if not results and len(q) >= 2:
        try:
            sym = q_upper
            yf_sym = sym if ("." in sym or sym.startswith("^")) else f"{sym}.NS"
            ticker = yf.Ticker(yf_sym)
            info = ticker.info
            long_name = info.get("longName") or info.get("shortName")
            if long_name:
                results = [{"symbol": sym, "name": long_name, "exchange": "NSE", "sector": info.get("sector", "")}]
        except Exception:
            pass

    return {"results": results[:20]}


@router.get("/{symbol}/quote")
async def get_quote(symbol: str):
    """
    Get current price quote for a stock.
    Tries NSE free API first (fast, no rate limit), falls back to yfinance.
    """
    symbol = sanitize_symbol(symbol)
    cache_key = make_cache_key("stock:quote", symbol)

    cached = await cache_manager.get(cache_key)
    if cached:
        return cached

    # --- Strategy 1: NSE quote (fast, free, no rate limit) ---
    try:
        nse_data = await nse_fetch_quote(symbol)
        if nse_data and nse_data.get("lastPrice"):
            # Look up friendly name from our index
            name = symbol
            for s in _SEARCH_INDEX:
                if s["symbol"] == symbol:
                    name = s["name"]
                    break

            result = {
                "symbol": symbol,
                "price": safe_float(nse_data["lastPrice"]),
                "change": safe_float(nse_data.get("change")),
                "change_pct": safe_float(nse_data.get("pChange")),
                "volume": safe_float(nse_data.get("totalTradedVolume")),
                "high": safe_float(nse_data.get("high")),
                "low": safe_float(nse_data.get("low")),
                "open": safe_float(nse_data.get("open")),
                "prev_close": safe_float(nse_data.get("previousClose")),
                "name": name,
                "market_cap": None,
            }
            await cache_manager.set(cache_key, result, ttl=timedelta(minutes=2))
            return result
    except Exception as e:
        logger.debug("NSE quote failed for %s, trying yfinance: %s", symbol, e)

    # --- Strategy 2: yfinance fallback ---
    try:
        hist = await async_fetch_history(symbol, period="5d", interval="1d")

        if hist is None or hist.empty:
            raise HTTPException(status_code=404, detail=f"No data found for {symbol}")

        current = safe_float(hist["Close"].iloc[-1])
        prev_close = safe_float(hist["Close"].iloc[-2]) if len(hist) > 1 else None
        change = round(current - prev_close, 2) if current and prev_close else None
        change_pct = round((change / prev_close) * 100, 2) if change and prev_close else None

        # Get stock name from MAJOR_STOCKS index (avoid slow yfinance .info call)
        name = symbol
        for s in _SEARCH_INDEX:
            if s["symbol"] == symbol:
                name = s["name"]
                break

        result = {
            "symbol": symbol,
            "price": current,
            "change": change,
            "change_pct": change_pct,
            "volume": safe_float(hist["Volume"].iloc[-1]) if "Volume" in hist.columns else None,
            "high": safe_float(hist["High"].iloc[-1]),
            "low": safe_float(hist["Low"].iloc[-1]),
            "open": safe_float(hist["Open"].iloc[-1]),
            "prev_close": prev_close,
            "name": name,
            "market_cap": None,
        }

        await cache_manager.set(cache_key, result, ttl=timedelta(minutes=2))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Quote error for %s: %s", symbol, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{symbol}/history")
async def get_history(symbol: str, period: str = "6mo", interval: str = "1d"):
    """Get OHLCV price history."""
    symbol = sanitize_symbol(symbol)
    cache_key = make_cache_key("stock:history", symbol, period=period, interval=interval)

    cached = await cache_manager.get(cache_key)
    if cached:
        return cached

    df = await async_fetch_history(symbol, period=period, interval=interval)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"No history found for {symbol}")

    # For intraday intervals, include full ISO timestamp; for daily+, date only
    is_intraday = interval in ("1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h")

    history = []
    for date, row in df.iterrows():
        if is_intraday:
            # Unix timestamp for intraday (lightweight-charts expects epoch seconds)
            ts = int(date.timestamp())
            date_val = ts
        else:
            date_val = str(date)[:10]
        history.append({
            "date": date_val,
            "o": safe_float(row.get("Open")),
            "h": safe_float(row.get("High")),
            "l": safe_float(row.get("Low")),
            "c": safe_float(row.get("Close")),
            "v": safe_float(row.get("Volume")),
        })

    result = {"symbol": symbol, "period": period, "interval": interval, "history": history}
    await cache_manager.set(cache_key, result, ttl=timedelta(minutes=30))
    return result


@router.get("/{symbol}/technicals")
async def get_technicals(symbol: str):
    """Get technical indicators for a stock."""
    symbol = sanitize_symbol(symbol)
    cache_key = make_cache_key("stock:technicals", symbol)

    cached = await cache_manager.get(cache_key)
    if cached:
        return cached

    df = await async_fetch_history(symbol, period="1y", interval="1d")
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"No data found for {symbol}")

    technicals = compute_technicals(df)
    sr = compute_support_resistance(df)
    fib = compute_fibonacci_levels(df)
    poc = compute_volume_profile_poc(df)
    regime = detect_market_regime(df) if len(df) >= 200 else None

    result = {
        "symbol": symbol,
        **technicals,
        "support_resistance": sr,
        "fibonacci": fib,
        "poc": poc,
        "market_regime": regime,
    }

    await cache_manager.set(cache_key, result, ttl=timedelta(minutes=30))
    return result

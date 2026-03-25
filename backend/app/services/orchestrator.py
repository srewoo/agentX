from __future__ import annotations
"""
Signal Orchestrator — the core background engine.
Flow: fetch data → run signal engine (deterministic) → filter by risk mode
      → 1 LLM call for top signal → store in SQLite → ready to serve.

Architecture rules enforced here:
- signal_engine NEVER calls LLM (ensured by import structure)
- LLM is called only once per scan cycle (max 1 enrichment)
- Data fetching is rate-limited with semaphore + delay
"""
import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import aiosqlite

from app.config import settings as app_settings
from app.database import DB_PATH
from app.services.data_fetcher import MAJOR_STOCKS, async_fetch_history
from app.services.screener import pre_screen_stocks
from app.services.technicals import compute_technicals, compute_support_resistance
from app.services.signal_engine import scan_symbol, filter_by_risk_mode
from app.services.llm_analyst import enrich_signal
from app.services.sentiment import get_stock_news, calculate_sentiment
from app.services.cache import cache_manager, make_cache_key
from app.services.signal_tracker import evaluate_signals
from app.services.alert_checker import check_alerts

logger = logging.getLogger(__name__)

# IST timezone offset
IST = timezone(timedelta(hours=5, minutes=30))

# State shared between orchestrator and API routes
last_scan_time: Optional[str] = None
last_scan_signal_count: int = 0


def is_market_open() -> bool:
    """Check if NSE/BSE is currently open (9:15 AM - 3:30 PM IST, Mon-Fri)."""
    now_ist = datetime.now(IST)
    if now_ist.weekday() >= 5:  # Saturday or Sunday
        return False
    market_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now_ist <= market_close


async def _get_settings() -> dict[str, Any]:
    """Load current settings from database."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT key, value FROM settings") as cursor:
                rows = await cursor.fetchall()
                return {row["key"]: row["value"] for row in rows}
    except Exception as e:
        logger.error(f"Failed to load settings: {e}")
        return {}


async def _get_watchlist_symbols() -> list[str]:
    """Get watchlist symbols from database."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT symbol FROM watchlist") as cursor:
                rows = await cursor.fetchall()
                return [row["symbol"] for row in rows]
    except Exception as e:
        logger.error(f"Failed to load watchlist: {e}")
        return []


async def _store_signals(signals: list[dict]) -> None:
    """Persist signals to SQLite."""
    if not signals:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        for sig in signals:
            await db.execute(
                """INSERT OR REPLACE INTO signals
                   (id, symbol, signal_type, direction, strength, reason, risk,
                    llm_summary, current_price, metadata, created_at, read, dismissed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sig["id"], sig["symbol"], sig["signal_type"], sig["direction"],
                    sig["strength"], sig["reason"], sig.get("risk"),
                    sig.get("llm_summary"), sig.get("current_price"),
                    json.dumps(sig.get("metadata", {})),
                    sig["created_at"], 0, 0,
                ),
            )
        await db.commit()
    logger.info(f"Stored {len(signals)} signals")


async def _get_previous_prices() -> dict[str, float]:
    """Load last known prices from cache for price-spike detection."""
    cached = await cache_manager.get("orchestrator:prev_prices")
    return cached or {}


async def _store_current_prices(prices: dict[str, float]) -> None:
    await cache_manager.set(
        "orchestrator:prev_prices", prices, ttl=timedelta(hours=2)
    )


async def run_scan_cycle() -> list[dict]:
    """
    Execute one full scan cycle.
    Returns list of signals generated.
    """
    global last_scan_time, last_scan_signal_count

    start_time = datetime.now(timezone.utc)
    logger.info("Starting scan cycle...")

    db_settings = await _get_settings()
    risk_mode = db_settings.get("risk_mode", "balanced")
    signal_types_raw = db_settings.get("signal_types", '["intraday","swing","long_term"]')
    try:
        signal_types = json.loads(signal_types_raw)
    except Exception:
        signal_types = ["intraday", "swing", "long_term"]

    # Build scan list: watchlist + TradingView pre-screened stocks (momentum/volume/price extremes).
    # This replaces scanning ALL 160+ MAJOR_STOCKS — only scan watchlist + pre-screened.
    watchlist_symbols = await _get_watchlist_symbols()
    watchlist_set = set(watchlist_symbols)

    # Pre-screen via TradingView to find stocks with interesting signals
    pre_screened: list[str] = []
    try:
        loop = asyncio.get_event_loop()
        pre_screened = await loop.run_in_executor(None, pre_screen_stocks)
        logger.info(f"TradingView pre-screen returned {len(pre_screened)} candidates")
    except Exception as e:
        logger.warning(f"TradingView pre-screen failed, falling back to MAJOR_STOCKS: {e}")

    # Build final scan list: watchlist (priority) + pre-screened + fallback to MAJOR_STOCKS
    seen: set[str] = set()
    all_symbols: list[str] = []

    # 1. Watchlist first (highest priority)
    for sym in watchlist_set:
        if sym not in seen:
            all_symbols.append(sym)
            seen.add(sym)

    # 2. Pre-screened stocks from TradingView
    for sym in pre_screened:
        if sym not in seen:
            all_symbols.append(sym)
            seen.add(sym)

    # 3. Fallback: if pre-screening returned nothing, use MAJOR_STOCKS
    if not pre_screened:
        for s in MAJOR_STOCKS:
            if s["symbol"] not in seen:
                all_symbols.append(s["symbol"])
                seen.add(s["symbol"])

    previous_prices = await _get_previous_prices()
    current_prices: dict[str, float] = {}
    all_signals: list[dict] = []

    # Rate-limited concurrent fetching (max 5 concurrent yfinance calls)
    semaphore = asyncio.Semaphore(5)

    async def process_symbol(sym: str) -> list[dict]:
        async with semaphore:
            try:
                df = await async_fetch_history(sym, period="6mo", interval="1d")
                if df is None or df.empty or len(df) < 20:
                    return []

                technicals = compute_technicals(df)
                if not technicals:
                    return []

                sr = compute_support_resistance(df)
                prev_price = previous_prices.get(sym)

                # For watchlist stocks, also check news sentiment
                sentiment_score = None
                if sym in watchlist_set:
                    try:
                        news = await get_stock_news(sym, limit=5)
                        if news:
                            scores = [a["sentiment_score"] for a in news]
                            sentiment_score = sum(scores) / len(scores)
                    except Exception:
                        pass

                current_price = technicals.get("current_price")
                if current_price:
                    current_prices[sym] = current_price

                return scan_symbol(
                    symbol=sym,
                    df=df,
                    technicals=technicals,
                    sr=sr,
                    previous_price=prev_price,
                    sentiment_score=sentiment_score,
                )
            except Exception as e:
                logger.warning(f"Error scanning {sym}: {e}")
                return []
            finally:
                # Slight delay to avoid hammering yfinance
                await asyncio.sleep(0.3)

    # Run all symbols concurrently (semaphore limits actual parallelism)
    results = await asyncio.gather(*[process_symbol(sym) for sym in all_symbols])
    for result in results:
        all_signals.extend(result)

    # Save current prices for next cycle's spike detection
    await _store_current_prices(current_prices)

    # Check price alerts against current prices
    try:
        triggered_alert_signals = await check_alerts(current_prices)
        if triggered_alert_signals:
            all_signals.extend(triggered_alert_signals)
            logger.info(f"Price alerts triggered: {len(triggered_alert_signals)}")
    except Exception as e:
        logger.warning(f"Price alert check failed (non-critical): {e}")

    # Filter by risk mode
    filtered = filter_by_risk_mode(all_signals, risk_mode)

    # Sort by strength desc; watchlist signals get priority boost
    for sig in filtered:
        if sig["symbol"] in watchlist_set:
            sig["strength"] = min(10, sig["strength"] + 1)
    filtered.sort(key=lambda s: s["strength"], reverse=True)

    # LLM enrichment: max 1 call per cycle, for the strongest signal
    if filtered:
        top_signal = filtered[0]
        try:
            # Refetch technicals for the top signal symbol for LLM context
            df_top = await async_fetch_history(top_signal["symbol"], period="6mo", interval="1d")
            technicals_top = compute_technicals(df_top) if df_top is not None and not df_top.empty else {}
            summary = await enrich_signal(top_signal, technicals_top, db_settings)
            if summary:
                top_signal["llm_summary"] = summary
                logger.info(f"Enriched signal for {top_signal['symbol']} with LLM narrative")
        except Exception as e:
            logger.warning(f"LLM enrichment failed: {e}")

    # Persist to DB
    await _store_signals(filtered)

    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    last_scan_time = datetime.now(timezone.utc).isoformat()
    last_scan_signal_count = len(filtered)

    logger.info(
        f"Scan complete: {len(all_symbols)} symbols, {len(filtered)} signals, "
        f"{elapsed:.1f}s elapsed. Market {'open' if is_market_open() else 'closed'}."
    )

    # Evaluate outcomes of older signals (non-blocking best-effort)
    try:
        eval_result = await evaluate_signals()
        logger.info(f"Signal evaluation after scan: {eval_result}")
    except Exception as e:
        logger.warning(f"Signal evaluation failed (non-critical): {e}")

    return filtered


class SignalOrchestrator:
    """Background scheduler that runs scan cycles on a configurable interval."""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Signal orchestrator started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Signal orchestrator stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                db_settings = await _get_settings()
                interval_minutes = int(db_settings.get("alert_interval_minutes", app_settings.default_alert_interval_minutes))

                await run_scan_cycle()

                # Wait for next cycle
                await asyncio.sleep(interval_minutes * 60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Orchestrator loop error: {e}")
                # Back off before retry
                await asyncio.sleep(60)

    def is_running(self) -> bool:
        return self._running


# Global orchestrator instance
orchestrator = SignalOrchestrator()

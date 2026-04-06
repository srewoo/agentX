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
from app.services.data_fetcher import MAJOR_STOCKS, async_fetch_history, get_delivery_volume
from app.services.screener import pre_screen_stocks
from app.services.technicals import compute_technicals, compute_support_resistance
from app.services.signal_engine import scan_symbol, filter_by_risk_mode, detect_options_signal
from app.services.llm_analyst import enrich_signal
from app.services.sentiment import get_stock_news, get_sentiment_summary, calculate_sentiment
from app.services.fundamentals import get_fundamentals
from app.services.cache import cache_manager, make_cache_key
from app.services.signal_tracker import evaluate_signals
from app.services.alert_checker import check_alerts
from app.services.fii_dii import get_fii_dii_data, get_signal_strength_modifier as fii_strength_modifier
from app.services.market_rules import get_fno_ban_list, should_suppress_signal
from app.services.relative_strength import compute_relative_strength, get_rs_strength_modifier
from app.services.corporate_governance import get_promoter_pledge_data, get_pledge_strength_modifier
from app.services.market_data import (
    get_india_vix, get_vix_adjusted_thresholds, get_upcoming_results_dates,
    get_option_chain_analysis, refresh_earnings_calendar,
)
from app.services.market_regime import detect_market_regime

logger = logging.getLogger(__name__)

# IST timezone offset
IST = timezone(timedelta(hours=5, minutes=30))

# State shared between orchestrator and API routes
last_scan_time: Optional[str] = None
last_scan_signal_count: int = 0

# Track symbols that consistently fail — skip them to avoid wasting time
_bad_symbol_strikes: dict[str, int] = {}  # symbol → consecutive failure count
_BAD_SYMBOL_THRESHOLD = 3  # skip after 3 consecutive failures
_BAD_SYMBOL_MAX_CACHE = 500  # prevent unbounded growth

# Minimum price filter — avoid penny stocks with wide bid-ask spreads,
# operator manipulation risk, and unrealistic fill assumptions.
# Configurable via settings table key "min_stock_price".
_DEFAULT_MIN_STOCK_PRICE = 10.0

# Sector concentration — max open positions per sector
_MAX_SECTOR_POSITIONS = 2


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

    # Load user-configurable thresholds for signal detectors
    scan_thresholds = {
        "rsi_overbought": db_settings.get("rsi_overbought", "70"),
        "rsi_oversold": db_settings.get("rsi_oversold", "30"),
        "price_spike_pct": db_settings.get("price_spike_pct", "3.0"),
        "volume_spike_ratio": db_settings.get("volume_spike_ratio", "2.0"),
        "breakout_min_score": db_settings.get("breakout_min_score", "4"),
    }

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

    # ── Fetch market-wide context in parallel (FII/DII, VIX, F&O ban, earnings) ─
    fii_dii_data: dict = {}
    india_vix: Optional[float] = None
    fno_ban_set: set[str] = set()

    async def _fetch_fii():
        return await get_fii_dii_data()

    async def _fetch_vix():
        return await get_india_vix()

    async def _fetch_ban():
        return await get_fno_ban_list()

    async def _fetch_earnings():
        return await refresh_earnings_calendar()

    ctx_results = await asyncio.gather(
        _fetch_fii(), _fetch_vix(), _fetch_ban(), _fetch_earnings(),
        return_exceptions=True,
    )

    if not isinstance(ctx_results[0], BaseException) and ctx_results[0]:
        fii_dii_data = ctx_results[0]
        logger.info("FII net: %s Cr | DII net: %s Cr | Sentiment: %s",
                    fii_dii_data.get("fii_net"), fii_dii_data.get("dii_net"), fii_dii_data.get("sentiment"))

    if not isinstance(ctx_results[1], BaseException) and ctx_results[1] is not None:
        india_vix = ctx_results[1]
        logger.info("India VIX: %.2f", india_vix)

    if not isinstance(ctx_results[2], BaseException) and ctx_results[2]:
        fno_ban_set = ctx_results[2]
        logger.info("F&O ban list: %d symbols", len(fno_ban_set))

    if not isinstance(ctx_results[3], BaseException) and ctx_results[3]:
        logger.info("Earnings calendar: %d symbols with upcoming results", len(ctx_results[3]))

    # ── Regime-adaptive signal parameters (NIFTY 50 market regime detection) ─
    try:
        nifty_df = await async_fetch_history("^NSEI", period="1y", interval="1d")
        if nifty_df is not None and not nifty_df.empty and len(nifty_df) >= 200:
            regime_info = detect_market_regime(nifty_df)
            regime = regime_info.get("regime", "Unknown")
            logger.info("Market regime: %s (confidence: %d%%)", regime, regime_info.get("confidence", 0))

            # Regime-adaptive threshold overrides
            _REGIME_THRESHOLDS: dict[str, dict] = {
                "Strong Bull": {"price_spike_pct": "4.0", "volume_spike_ratio": "2.5", "rsi_overbought": "80", "rsi_oversold": "25", "breakout_min_score": "5"},
                "Strong Bear": {"price_spike_pct": "4.0", "volume_spike_ratio": "2.5", "rsi_overbought": "65", "rsi_oversold": "35", "breakout_min_score": "5"},
                "Ranging":     {"price_spike_pct": "2.5", "volume_spike_ratio": "1.5", "rsi_overbought": "70", "rsi_oversold": "30", "breakout_min_score": "3"},
                "Volatile":    {"price_spike_pct": "6.0", "volume_spike_ratio": "3.0", "rsi_overbought": "75", "rsi_oversold": "25", "breakout_min_score": "6"},
                "Weak Bull":   {"price_spike_pct": "3.0", "volume_spike_ratio": "2.0", "rsi_overbought": "72", "rsi_oversold": "28", "breakout_min_score": "4"},
                "Weak Bear":   {"price_spike_pct": "3.0", "volume_spike_ratio": "2.0", "rsi_overbought": "68", "rsi_oversold": "32", "breakout_min_score": "4"},
            }
            if regime in _REGIME_THRESHOLDS:
                # Regime-adaptive overrides take precedence over user settings
                regime_t = _REGIME_THRESHOLDS[regime]
                scan_thresholds.update(regime_t)
                logger.info("Applied regime-adaptive thresholds for %s regime", regime)

            # In Volatile regime, auto-upgrade to conservative filtering
            if regime == "Volatile" and risk_mode == "aggressive":
                risk_mode = "balanced"
                logger.info("Volatile regime detected — auto-upgrading aggressive to balanced risk mode")
    except Exception as e:
        logger.debug("Regime detection failed (non-critical): %s", e)

    # Adjust signal thresholds based on India VIX regime
    if india_vix:
        scan_thresholds = get_vix_adjusted_thresholds(india_vix, scan_thresholds)
        logger.info("VIX-adjusted thresholds (VIX=%.1f): %s", india_vix, scan_thresholds)

    previous_prices = await _get_previous_prices()
    current_prices: dict[str, float] = {}
    all_signals: list[dict] = []

    # Minimum price threshold (configurable via settings)
    min_price = float(db_settings.get("min_stock_price", _DEFAULT_MIN_STOCK_PRICE))

    # Build sector lookup from MAJOR_STOCKS for sector concentration check
    _sector_lookup: dict[str, str] = {s["symbol"]: s.get("sector", "Unknown") for s in MAJOR_STOCKS}

    # Rate-limited concurrent fetching (max 3 concurrent to avoid yfinance rate limits)
    semaphore = asyncio.Semaphore(3)

    async def process_symbol(sym: str) -> list[dict]:
        async with semaphore:
            # Skip symbols that consistently fail (delisted, bad ticker, etc.)
            if _bad_symbol_strikes.get(sym, 0) >= _BAD_SYMBOL_THRESHOLD:
                return []

            try:
                df = await async_fetch_history(sym, period="6mo", interval="1d")
                if df is None or df.empty or len(df) < 20:
                    _bad_symbol_strikes[sym] = _bad_symbol_strikes.get(sym, 0) + 1
                    if _bad_symbol_strikes[sym] == _BAD_SYMBOL_THRESHOLD:
                        logger.info("Skipping %s in future scans (%d consecutive failures)", sym, _BAD_SYMBOL_THRESHOLD)
                    return []

                # Success — reset strike count
                _bad_symbol_strikes.pop(sym, None)

                technicals = compute_technicals(df)
                if not technicals:
                    return []

                # ── Minimum price filter ──────────────────────────────────────
                current_price = technicals.get("current_price")
                if current_price and current_price < min_price:
                    logger.debug("Skipping %s — price Rs.%.2f below minimum Rs.%.2f", sym, current_price, min_price)
                    return []

                # ── F&O ban filter ────────────────────────────────────────────
                if sym in fno_ban_set:
                    logger.debug("Skipping %s — in F&O ban period", sym)
                    return []

                # ── Earnings blackout period ──────────────────────────────────
                try:
                    earnings_info = await get_upcoming_results_dates(sym, window_days=3)
                    if earnings_info.get("has_upcoming_results"):
                        days = earnings_info.get("days_to_event", 0)
                        logger.debug("Skipping %s — earnings/results in %d days", sym, days)
                        return []
                except Exception:
                    pass  # Non-critical — proceed without earnings check

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

                if current_price:
                    current_prices[sym] = current_price

                # Fetch delivery volume % only if volume looks elevated
                # (avoid one extra API call per symbol — only fetch when it matters)
                sym_delivery_pct = None
                vol_current = technicals.get("volume_current")
                vol_avg = technicals.get("volume_avg_20")
                if vol_current and vol_avg and vol_avg > 0 and (vol_current / vol_avg) >= 1.5:
                    try:
                        delivery_data = await get_delivery_volume(sym)
                        sym_delivery_pct = delivery_data.get("delivery_pct")
                    except Exception:
                        pass

                return scan_symbol(
                    symbol=sym,
                    df=df,
                    technicals=technicals,
                    sr=sr,
                    previous_price=prev_price,
                    sentiment_score=sentiment_score,
                    thresholds=scan_thresholds,
                    delivery_pct=sym_delivery_pct,
                )
            except Exception as e:
                _bad_symbol_strikes[sym] = _bad_symbol_strikes.get(sym, 0) + 1
                logger.debug("Error scanning %s (strike %d): %s", sym, _bad_symbol_strikes[sym], e)
                return []
            finally:
                # Delay between symbols to avoid yfinance rate limits
                await asyncio.sleep(0.5)

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

    # ── Options flow signals for NIFTY 50 stocks (F&O eligible) ─────────────
    # Run for top-ranked symbols that had directional signals (cap at 5 to limit API calls)
    _FNO_ELIGIBLE = {s["symbol"] for s in MAJOR_STOCKS[:50]}  # First 50 are NIFTY 50
    symbols_with_signals = list({s["symbol"] for s in all_signals if s["symbol"] in _FNO_ELIGIBLE})[:5]

    for sym in symbols_with_signals:
        try:
            options_data = await get_option_chain_analysis(sym)
            if options_data:
                current_p = current_prices.get(sym)
                opt_sig = detect_options_signal(sym, current_p, options_data)
                if opt_sig:
                    all_signals.append(opt_sig)
                    logger.info("Options signal detected for %s: %s", sym, opt_sig.get("reason", "")[:60])
        except Exception as e:
            logger.debug("Options signal detection failed for %s: %s", sym, e)

    # ── Apply FII/DII strength modifier to all signals ───────────────────────
    if fii_dii_data.get("fii_net") is not None:
        for sig in all_signals:
            modifier = fii_strength_modifier(fii_dii_data, sig.get("direction", "neutral"))
            if modifier != 0:
                sig["strength"] = max(1, min(10, sig["strength"] + modifier))
                sig["metadata"] = sig.get("metadata", {})
                sig["metadata"]["fii_modifier"] = modifier
                sig["metadata"]["fii_net"] = fii_dii_data.get("fii_net")

    # ── Relative strength modifier (batch-computed, cap symbols to avoid yfinance overload) ─
    signal_symbols = list({s["symbol"] for s in all_signals})[:20]  # cap at 20 to limit yfinance calls
    rs_rankings: dict = {}
    if signal_symbols:
        try:
            rs_result = await compute_relative_strength(signal_symbols, period="3mo")
            rs_rankings = rs_result.get("rankings", {})
            if rs_rankings:
                logger.info("Relative strength computed for %d symbols", len(rs_rankings))
        except Exception as e:
            logger.debug("Relative strength computation failed (non-critical): %s", e)

    if rs_rankings:
        for sig in all_signals:
            sym_rs = rs_rankings.get(sig["symbol"])
            if sym_rs:
                rs_mod = get_rs_strength_modifier(sym_rs.get("rs_rank"))
                if rs_mod != 0:
                    sig["strength"] = max(1, min(10, sig["strength"] + rs_mod))
                    sig["metadata"] = sig.get("metadata", {})
                    sig["metadata"]["rs_rank"] = sym_rs.get("rs_rank")
                    sig["metadata"]["rs_modifier"] = rs_mod

    # ── Promoter pledge modifier (for top signal during enrichment) ──────────
    # Applied below after filtering, only to the top signal to avoid N API calls.

    # Filter by risk mode
    filtered = filter_by_risk_mode(all_signals, risk_mode)

    # Sort by strength desc; watchlist signals get priority boost
    for sig in filtered:
        if sig["symbol"] in watchlist_set:
            sig["strength"] = min(10, sig["strength"] + 1)
    filtered.sort(key=lambda s: s["strength"], reverse=True)

    # ── Sector concentration deduplication ───────────────────────────────────
    # If 3+ signals fire for the same sector, keep only the strongest 2.
    # This prevents the system from flooding signals on banking/IT when the
    # whole sector moves, which would all be correlated positions.
    sector_count: dict[str, int] = {}
    sector_deduplicated: list[dict] = []
    for sig in filtered:
        sym = sig["symbol"]
        sector = _sector_lookup.get(sym, "Unknown")
        sector_count[sector] = sector_count.get(sector, 0) + 1
        if sector_count[sector] <= _MAX_SECTOR_POSITIONS:
            sector_deduplicated.append(sig)
        else:
            logger.debug(
                "Sector concentration limit: dropping %s (%s) — already have %d signals in %s sector",
                sym, sig["signal_type"], _MAX_SECTOR_POSITIONS, sector,
            )
    filtered = sector_deduplicated

    # LLM enrichment: max 1 call per cycle, for the strongest signal
    if filtered:
        top_signal = filtered[0]

        # ── Promoter pledge check on top signal ──────────────────────
        try:
            pledge_data = await get_promoter_pledge_data(top_signal["symbol"])
            pledge_mod = get_pledge_strength_modifier(pledge_data)
            if pledge_mod != 0:
                top_signal["strength"] = max(1, min(10, top_signal["strength"] + pledge_mod))
                top_signal["metadata"] = top_signal.get("metadata", {})
                top_signal["metadata"]["pledge_risk"] = pledge_data.get("risk_level")
                top_signal["metadata"]["pledge_modifier"] = pledge_mod
                logger.info("Pledge modifier for %s: %d (risk: %s)", top_signal["symbol"], pledge_mod, pledge_data.get("risk_level"))
        except Exception as e:
            logger.debug("Pledge check failed for %s: %s", top_signal["symbol"], e)

        try:
            # Refetch technicals for the top signal symbol for LLM context
            df_top = await async_fetch_history(top_signal["symbol"], period="6mo", interval="1d")
            technicals_top = compute_technicals(df_top) if df_top is not None and not df_top.empty else {}

            # Fetch fundamentals for the top signal (best-effort)
            top_fundamentals = None
            try:
                top_fundamentals = await get_fundamentals(top_signal["symbol"])
            except Exception as e:
                logger.debug(f"Fundamentals fetch failed for {top_signal['symbol']}: {e}")

            # Fetch sentiment for the top signal (best-effort)
            top_sentiment = None
            try:
                news = await get_stock_news(top_signal["symbol"], limit=5)
                if news:
                    scores = [a["sentiment_score"] for a in news]
                    top_sentiment = sum(scores) / len(scores)
            except Exception as e:
                logger.debug(f"Sentiment fetch failed for {top_signal['symbol']}: {e}")

            summary = await enrich_signal(
                top_signal,
                technicals_top,
                db_settings,
                fundamentals=top_fundamentals,
                sentiment_score=top_sentiment,
            )
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

    # Trim bad symbol cache to prevent unbounded growth
    if len(_bad_symbol_strikes) > _BAD_SYMBOL_MAX_CACHE:
        # Keep only the ones at threshold (confirmed bad); drop low-strike entries
        to_remove = [s for s, n in _bad_symbol_strikes.items() if n < _BAD_SYMBOL_THRESHOLD]
        for s in to_remove[:len(_bad_symbol_strikes) - _BAD_SYMBOL_MAX_CACHE // 2]:
            del _bad_symbol_strikes[s]

    skipped = sum(1 for n in _bad_symbol_strikes.values() if n >= _BAD_SYMBOL_THRESHOLD)
    logger.info(
        f"Scan complete: {len(all_symbols)} symbols scanned, {skipped} skipped (bad), "
        f"{len(filtered)} signals, {elapsed:.1f}s elapsed. "
        f"Market {'open' if is_market_open() else 'closed'}."
    )

    # Evaluate outcomes of older signals (non-blocking best-effort)
    try:
        eval_result = await evaluate_signals()
        logger.info(f"Signal evaluation after scan: {eval_result}")
    except Exception as e:
        logger.warning(f"Signal evaluation failed (non-critical): {e}")

    # Cleanup old signals (non-blocking best-effort)
    try:
        from app.database import cleanup_old_signals
        deleted = await cleanup_old_signals()
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} old signals")
    except Exception as e:
        logger.warning(f"Signal cleanup failed (non-critical): {e}")

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

                # 0 = manual-only mode: idle without scanning
                if interval_minutes == 0:
                    await asyncio.sleep(60)
                    continue

                if not is_market_open():
                    logger.info("Market closed — skipping auto-scan")
                    await asyncio.sleep(interval_minutes * 60)
                    continue

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

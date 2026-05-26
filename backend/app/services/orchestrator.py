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
from dataclasses import dataclass, field
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
from app.services.llm_signal_judge import judge_signals, is_enabled as judge_enabled
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
from app.services.recommendation_calibration import run_large_scale_calibration

logger = logging.getLogger(__name__)

# IST timezone offset
IST = timezone(timedelta(hours=5, minutes=30))

# State shared between orchestrator and API routes.
#
# `_ScanState` wraps the scan-cycle state in a dataclass guarded by an
# `asyncio.Lock` so concurrent scan triggers cannot race on the writes at the
# end of `run_scan_cycle`. The module-level `last_scan_time` /
# `last_scan_signal_count` names are kept as read-only mirrors of the state
# for existing importers (e.g. `app.routers.market`).
@dataclass
class _ScanState:
    """Mutable scan-cycle state shared across scan triggers.

    Backs both the periodic scheduler loop and the manual `/api/scan/trigger`
    endpoint. The manual trigger is asynchronous: POST returns 202 with
    `job_id`, the client polls `/api/scan/status` for progress + result.
    This avoids the 120s HTTP timeout that was killing real 160-200s scans.
    """

    last_scan_time: Optional[str] = None
    last_scan_signal_count: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ── Manual-trigger job tracking ────────────────────────────────────
    job_id: Optional[str] = None
    status: str = "idle"            # idle | running | completed | failed
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_ms: Optional[int] = None
    # Progress: filled in by run_scan_cycle as it iterates symbols.
    total_symbols: int = 0
    completed_symbols: int = 0
    current_symbol: Optional[str] = None
    signals_so_far: int = 0
    error: Optional[str] = None


_scan_state = _ScanState()
last_scan_time: Optional[str] = None
last_scan_signal_count: int = 0

# Module A runs at most once per IST trading day; this latch prevents
# the scan loop (which fires every N minutes) from triggering 30+
# Module A scans per day.
_module_a_last_run_date: Optional[str] = None


def _module_a_ran_today() -> bool:
    global _module_a_last_run_date
    today = datetime.now(IST).date().isoformat()
    return _module_a_last_run_date == today


def _mark_module_a_ran_today() -> None:
    global _module_a_last_run_date
    _module_a_last_run_date = datetime.now(IST).date().isoformat()


# Concurrency cap for the weekly backtest fan-out. Tuned to keep yfinance /
# NSE rate limits happy while still finishing ~50 symbols in reasonable time.
_BACKTEST_CONCURRENCY = 5
_BACKTEST_PER_SYMBOL_TIMEOUT = 30.0  # seconds — backtests that exceed this are skipped

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
    """Load current settings from database.

    SECRET_KEYS rows are stored sealed (encrypted at rest). Internal
    consumers — the LLM judge, the analyst, notification channels —
    expect plaintext. Unseal here so callers don't have to know the
    table layout. Without this, LLM providers get the literal
    ``enc:v1:...`` ciphertext as an API key and return 401.
    """
    try:
        from app.services.secrets import SECRET_KEYS, get_manager
        mgr = get_manager()
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT key, value FROM settings") as cursor:
                rows = await cursor.fetchall()
                out: dict[str, Any] = {}
                for row in rows:
                    k, v = row["key"], row["value"]
                    if k in SECRET_KEYS and v:
                        try:
                            v = mgr.unseal_key(v)
                        except Exception as e:
                            # Don't let a single bad ciphertext kill the whole
                            # scan. Log and pass through — the provider will
                            # 401 and the judge fails open.
                            logger.warning("Failed to unseal %s: %s", k, e)
                    out[k] = v
                return out
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


async def _get_watchlist_exchanges() -> dict[str, str]:
    """Return {symbol: exchange} for every watchlist row. Defaults to NSE."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT symbol, exchange FROM watchlist") as cursor:
                rows = await cursor.fetchall()
                return {
                    row["symbol"]: (row["exchange"] or "NSE").upper()
                    for row in rows
                }
    except Exception as e:
        logger.debug("Failed to load watchlist exchanges: %s", e)
        return {}


async def start_manual_scan() -> dict:
    """Spawn a scan in the background and return immediately with a job_id.

    The popup polls `get_scan_status()` for progress + completion. This
    replaces the synchronous `await run_scan_cycle()` path that hit the
    proxy/HTTP 120-240s timeout on real scans. We keep at most one
    in-flight manual scan — re-triggering while running returns the
    running job's id (idempotent).
    """
    import uuid

    async with _scan_state.lock:
        if _scan_state.status == "running" and _scan_state.job_id:
            # Idempotent retrigger — surface the in-flight job.
            return {
                "job_id": _scan_state.job_id,
                "status": "running",
                "started_at": _scan_state.started_at,
                "already_running": True,
            }
        _scan_state.job_id = uuid.uuid4().hex[:12]
        _scan_state.status = "running"
        _scan_state.started_at = datetime.now(timezone.utc).isoformat()
        _scan_state.completed_at = None
        _scan_state.duration_ms = None
        _scan_state.total_symbols = 0
        _scan_state.completed_symbols = 0
        _scan_state.current_symbol = None
        _scan_state.signals_so_far = 0
        _scan_state.error = None
        job_id = _scan_state.job_id
        started = _scan_state.started_at

    async def _run() -> None:
        import time
        t0 = time.time()
        try:
            await run_scan_cycle()
            async with _scan_state.lock:
                _scan_state.status = "completed"
                _scan_state.completed_at = datetime.now(timezone.utc).isoformat()
                _scan_state.duration_ms = int((time.time() - t0) * 1000)
        except Exception as e:
            logger.exception("Manual scan job %s failed: %s", job_id, e)
            async with _scan_state.lock:
                _scan_state.status = "failed"
                _scan_state.error = str(e)
                _scan_state.completed_at = datetime.now(timezone.utc).isoformat()
                _scan_state.duration_ms = int((time.time() - t0) * 1000)

    # Detach the task from the request — it survives the 202 response.
    asyncio.create_task(_run())

    return {
        "job_id": job_id,
        "status": "running",
        "started_at": started,
        "already_running": False,
    }


async def get_scan_status() -> dict:
    """Snapshot of the in-flight (or last) manual scan job for /api/scan/status."""
    async with _scan_state.lock:
        total = _scan_state.total_symbols
        done = _scan_state.completed_symbols
        progress_pct = round((done / total) * 100, 1) if total else 0.0
        return {
            "job_id": _scan_state.job_id,
            "status": _scan_state.status,
            "started_at": _scan_state.started_at,
            "completed_at": _scan_state.completed_at,
            "duration_ms": _scan_state.duration_ms,
            "total_symbols": total,
            "completed_symbols": done,
            "current_symbol": _scan_state.current_symbol,
            "progress_pct": progress_pct,
            "signals_so_far": _scan_state.signals_so_far,
            "error": _scan_state.error,
        }


async def _store_signals(signals: list[dict]) -> None:
    """Persist signals to SQLite."""
    if not signals:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        for sig in signals:
            await db.execute(
                """INSERT OR REPLACE INTO signals
                   (id, symbol, signal_type, direction, strength, reason, risk,
                    llm_summary, llm_verdict, llm_reason, exchange,
                    current_price, metadata, created_at, read, dismissed,
                    debate_winner, debate_synthesis, debate_confidence,
                    mp_aggregate_score, mp_consensus, mp_synthesis, mp_perspectives_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sig["id"], sig["symbol"], sig["signal_type"], sig["direction"],
                    sig["strength"], sig["reason"], sig.get("risk"),
                    sig.get("llm_summary"),
                    sig.get("llm_verdict"), sig.get("llm_reason"),
                    sig.get("exchange") or "NSE",
                    sig.get("current_price"),
                    json.dumps(sig.get("metadata", {})),
                    sig["created_at"], 0, 0,
                    sig.get("debate_winner"),
                    sig.get("debate_synthesis"),
                    sig.get("debate_confidence"),
                    sig.get("mp_aggregate_score"),
                    sig.get("mp_consensus"),
                    sig.get("mp_synthesis"),
                    sig.get("mp_perspectives_json"),
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
    # Per-symbol exchange map. Watchlist rows can be NSE or BSE; anything not
    # in the watchlist defaults to NSE (pre-screener + MAJOR_STOCKS are
    # NSE-listed). The scan routes data fetches accordingly.
    symbol_exchange = await _get_watchlist_exchanges()

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
                async with _scan_state.lock:
                    _scan_state.completed_symbols += 1
                return []

            # Lightweight progress hook — lets /api/scan/status report which
            # symbol is currently being scanned without taking the lock
            # inside the hot path of each detector.
            async with _scan_state.lock:
                _scan_state.current_symbol = sym

            try:
                sym_exchange = symbol_exchange.get(sym, "NSE")
                df = await async_fetch_history(
                    sym, period="6mo", interval="1d", exchange=sym_exchange
                )
                if df is None or df.empty or len(df) < 20:
                    _bad_symbol_strikes[sym] = _bad_symbol_strikes.get(sym, 0) + 1
                    if _bad_symbol_strikes[sym] == _BAD_SYMBOL_THRESHOLD:
                        logger.info("Skipping %s in future scans (%d consecutive failures)", sym, _BAD_SYMBOL_THRESHOLD)
                    return []

                # Success — reset strike count
                _bad_symbol_strikes.pop(sym, None)

                # Yield to the event loop after CPU-bound pandas work so that
                # lightweight requests (e.g. /api/scan/status polling) can be
                # served while the scan is running across 40+ concurrent tasks.
                technicals = compute_technicals(df)
                await asyncio.sleep(0)
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

                # ── Earnings handling ────────────────────────────────────────
                # The earnings calendar returns signed ``days_to_event``:
                #   - positive  → results upcoming → enter blackout, skip
                #   - negative  → results just happened → PEAD candidate window
                # Previously we skipped on *both* — which made PEAD impossible
                # to detect (the very setup we want to flag was being filtered
                # out before reaching the engine).
                earnings_recent_days_sym: Optional[int] = None
                try:
                    earnings_info = await get_upcoming_results_dates(sym, window_days=5)
                    if earnings_info.get("has_upcoming_results"):
                        days = earnings_info.get("days_to_event", 0)
                        if days is not None and days > 0:
                            # True upcoming-earnings blackout — skip scanning.
                            logger.debug("Skipping %s — earnings in %d days", sym, days)
                            return []
                        if days is not None and days <= 0:
                            # Results landed 0-5 sessions ago → PEAD candidate.
                            earnings_recent_days_sym = abs(days)
                except Exception:
                    pass  # Non-critical — proceed without earnings context

                sr = compute_support_resistance(df)
                await asyncio.sleep(0)
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
                vol_elevated = (
                    vol_current and vol_avg and vol_avg > 0
                    and (vol_current / vol_avg) >= 1.5
                )
                if vol_elevated:
                    try:
                        delivery_data = await get_delivery_volume(sym)
                        sym_delivery_pct = delivery_data.get("delivery_pct")
                    except Exception:
                        pass

                # ── Lazy fundamentals for PEAD / Quality Breakout ───────────
                # Fetch only when the symbol is *interesting* (in watchlist,
                # has elevated volume, OR has just-reported earnings). Skips
                # the rest of the universe entirely to keep cost bounded.
                sym_fundamentals = None
                interesting = (
                    sym in watchlist_set
                    or vol_elevated
                    or earnings_recent_days_sym is not None
                )
                if interesting:
                    try:
                        from app.services.fundamentals import get_fundamentals as _gf
                        sym_fundamentals = await _gf(sym, exchange=sym_exchange)
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
                    exchange=sym_exchange,
                    fundamentals=sym_fundamentals,
                    earnings_recent_days=earnings_recent_days_sym,
                )
            except Exception as e:
                _bad_symbol_strikes[sym] = _bad_symbol_strikes.get(sym, 0) + 1
                logger.debug("Error scanning %s (strike %d): %s", sym, _bad_symbol_strikes[sym], e)
                return []
            finally:
                # Delay between symbols to avoid yfinance rate limits
                await asyncio.sleep(0.5)
                async with _scan_state.lock:
                    _scan_state.completed_symbols += 1

    # Expose the symbol total so /api/scan/status can compute progress %.
    async with _scan_state.lock:
        _scan_state.total_symbols = len(all_symbols)
        _scan_state.completed_symbols = 0
        _scan_state.signals_so_far = 0

    # Run all symbols concurrently (semaphore limits actual parallelism)
    results = await asyncio.gather(*[process_symbol(sym) for sym in all_symbols])
    for result in results:
        all_signals.extend(result)
    async with _scan_state.lock:
        _scan_state.signals_so_far = len(all_signals)

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

    # Layer-2: optional LLM judge over the deterministic candidate list.
    # Single batched call per scan — fail-open so a provider outage or parse
    # error never blocks the deterministic signals from being stored.
    if judge_enabled(db_settings):
        try:
            verdicts = await judge_signals(filtered, db_settings)
            for sig in filtered:
                v = verdicts.get(sig["id"])
                if v is None:
                    continue
                sig["llm_verdict"] = v.verdict
                sig["llm_reason"] = v.reason
        except Exception as e:
            logger.warning("LLM judge layer failed (non-critical): %s", e)

    # Layer-3 (optional): bull/bear/judge debate over the strongest signals.
    # Gated on debate_enabled. Runs *after* the Layer-2 judge so dropped
    # signals (the LLM already vetoed them) don't waste extra LLM budget.
    try:
        from app.services.llm_debate import debate_top_signals, is_debate_enabled
        if is_debate_enabled(db_settings):
            # Skip already-dropped signals — no point debating something the
            # Layer-2 judge already vetoed.
            debate_pool = [s for s in filtered if s.get("llm_verdict") != "drop"]
            debate_results = await debate_top_signals(debate_pool, db_settings)
            for sig in filtered:
                r = debate_results.get(sig["id"])
                if r is None:
                    continue
                sig["debate_winner"] = r.verdict.winner
                sig["debate_synthesis"] = r.verdict.synthesis
                sig["debate_confidence"] = r.verdict.calibrated_confidence
    except Exception as e:
        logger.warning("Debate layer failed (non-critical): %s", e)

    # Layer-4 (optional): multi-perspective specialist analyst. Runs 4
    # LLM agents (technical / fundamental / sentiment / macro) + a
    # synthesiser on the top-N signals. Gated on multi_perspective_enabled.
    # Skip already-dropped signals — no point investing 5 LLM calls in a
    # setup the judge already vetoed.
    try:
        from app.services.llm_multi_perspective import (
            analyse_top_signals, is_multi_perspective_enabled,
        )
        if is_multi_perspective_enabled(db_settings):
            mp_pool = [s for s in filtered if s.get("llm_verdict") != "drop"]
            mp_results = await analyse_top_signals(mp_pool, db_settings)
            for sig in filtered:
                mpa = mp_results.get(sig["id"])
                if mpa is None:
                    continue
                sig["mp_aggregate_score"] = mpa.aggregate_score
                sig["mp_consensus"] = mpa.consensus
                sig["mp_synthesis"] = mpa.synthesis
                sig["mp_perspectives_json"] = json.dumps([
                    {
                        "perspective": p.perspective,
                        "score": p.score,
                        "confidence": p.confidence,
                        "summary": p.summary,
                    }
                    for p in mpa.perspectives
                ])
    except Exception as e:
        logger.warning("Multi-perspective analyst failed (non-critical): %s", e)

    # Persist to DB
    await _store_signals(filtered)

    # ── Auto-generate recommendations for top signals ─────────────────────
    # Without this, the `recommendation_outcomes` tracker only accumulates
    # rows when a user manually opens a stock detail — typically <5 a day.
    # The accuracy loop needs a steady stream, so each scan generates recos
    # for the top high-conviction non-dropped signals. Capped tight (3 per
    # scan) and cache-backed, so the marginal cost is bounded. HOLD/AVOID
    # recs are no-ops at the tracker layer (see store_recommendation).
    try:
        candidates_for_rec = [
            s for s in filtered
            if s.get("direction") in ("bullish", "bearish")
            and s.get("strength", 0) >= 7
            and s.get("llm_verdict") != "drop"
        ]
        candidates_for_rec.sort(key=lambda s: s.get("strength", 0), reverse=True)
        # Dedupe by symbol — one rec per stock per scan.
        seen_syms: set[str] = set()
        rec_targets: list[dict] = []
        for s in candidates_for_rec:
            sym = s["symbol"]
            if sym in seen_syms:
                continue
            seen_syms.add(sym)
            rec_targets.append(s)
            if len(rec_targets) >= 3:
                break
        if rec_targets:
            from app.services.recommendation import generate_recommendation
            for s in rec_targets:
                try:
                    await generate_recommendation(s["symbol"], horizon="swing")
                except Exception as e:
                    logger.debug(
                        "auto-reco for %s skipped: %s", s["symbol"], e,
                    )
            logger.info(
                "auto-reco: generated %d recommendations from top signals",
                len(rec_targets),
            )
    except Exception as e:
        # Tracker accumulation is a nice-to-have — never break the scan.
        logger.warning("auto-reco loop failed: %s", e)

    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    scan_ts = datetime.now(timezone.utc).isoformat()
    signal_count = len(filtered)
    async with _scan_state.lock:
        _scan_state.last_scan_time = scan_ts
        _scan_state.last_scan_signal_count = signal_count
        # Mirror to module globals so existing importers keep working.
        last_scan_time = scan_ts
        last_scan_signal_count = signal_count

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

    # Same for recommendation outcomes — the multi-factor engine has its
    # own self-improvement loop now; same cadence as signal_engine's so the
    # two paths stay in sync.
    try:
        from app.services.recommendation_tracker import evaluate_recommendation_outcomes
        rec_eval = await evaluate_recommendation_outcomes()
        if rec_eval.get("evaluated"):
            logger.info(f"Recommendation evaluation: {rec_eval}")
    except Exception as e:
        logger.warning(f"Recommendation evaluation failed (non-critical): {e}")

    # Auto paper-trading + Module A scan run as SEPARATE background tasks
    # (`_auto_paper_loop` and `_module_a_loop`). Keeping them out of
    # `run_scan_cycle` is critical: each does 40-152 yfinance calls and
    # was previously bloating scan duration past the 10s `/api/scan/status`
    # frontend timeout. The scan cycle now does only what its name says.

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
    """Background scheduler — scan cycles + weekly autonomous backtest."""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._backtest_task: Optional[asyncio.Task] = None
        self._calibration_task: Optional[asyncio.Task] = None
        self._auto_paper_task: Optional[asyncio.Task] = None
        self._module_a_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        self._backtest_task = asyncio.create_task(self._backtest_loop())
        self._calibration_task = asyncio.create_task(self._calibration_loop())
        # Separated from run_scan_cycle so /api/scan/status stays snappy.
        self._auto_paper_task = asyncio.create_task(self._auto_paper_loop())
        self._module_a_task = asyncio.create_task(self._module_a_loop())
        # Stop-loss monitor — own cadence, independent of the scan loop so
        # exits fire when the market breaches the stop rather than waiting
        # for the next 5-min scan cycle.
        from app.services.stop_loss_monitor import stop_loss_loop
        self._sl_monitor_task = asyncio.create_task(
            stop_loss_loop(poll_seconds=60, should_run=lambda: self._running)
        )
        await asyncio.sleep(0)
        logger.info("Signal orchestrator started (scan + weekly backtest + calibration)")

    async def stop(self) -> None:
        self._running = False
        for t in (
            self._task, self._backtest_task, self._calibration_task,
            getattr(self, "_auto_paper_task", None),
            getattr(self, "_module_a_task", None),
            getattr(self, "_sl_monitor_task", None),
        ):
            if t:
                t.cancel()
                try:
                    await t
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

    async def _backtest_loop(self) -> None:
        """Weekly autonomous backtest of NIFTY 50 — persisted for trend analysis.

        Sleeps until the next Sunday 18:00 IST, then runs a 1y/5d backtest on
        the watchlist + a curated NIFTY-50 subset. Results persist to the
        backtest_runs table; /api/performance/insights diffs runs to detect
        signal-engine drift over time.
        """
        # Initial delay — let the rest of the app finish booting before pulling
        # 50 stocks of history. 5 minutes is enough for any first-time setup.
        await asyncio.sleep(300)

        while self._running:
            try:
                next_run = _next_weekly_backtest_dt()
                wait_s = max(60, (next_run - datetime.now(timezone.utc)).total_seconds())
                logger.info("Next weekly backtest at %s UTC (in %.1fh)", next_run.isoformat(), wait_s / 3600)
                await asyncio.sleep(wait_s)
                if not self._running:
                    break
                await self._run_weekly_backtest()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Weekly backtest loop error: %s", e)
                await asyncio.sleep(3600)  # back off 1h on unexpected errors

    async def _calibration_loop(self) -> None:
        """Weekly large-scale recommendation calibration.

        The manual API still exists for ad-hoc runs. This loop makes the
        production path end-to-end: evaluate outcomes, calibrate factors over
        free EOD data, persist factor_performance, and seed the in-memory cache.
        """
        await asyncio.sleep(600)

        while self._running:
            try:
                next_run = _next_weekly_calibration_dt()
                wait_s = max(60, (next_run - datetime.now(timezone.utc)).total_seconds())
                logger.info(
                    "Next recommendation calibration at %s UTC (in %.1fh)",
                    next_run.isoformat(),
                    wait_s / 3600,
                )
                await asyncio.sleep(wait_s)
                if not self._running:
                    break
                await self._run_weekly_calibration()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Recommendation calibration loop error: %s", e)
                await asyncio.sleep(3600)

    async def _run_weekly_calibration(self) -> None:
        db_settings = await _get_settings()
        enabled = str(db_settings.get("recommendation_calibration_enabled", "true")).lower()
        if enabled not in {"1", "true", "yes", "on"}:
            logger.info("Recommendation calibration disabled by settings")
            return

        universe = db_settings.get("recommendation_calibration_universe", "nifty100")
        horizons = [
            h.strip()
            for h in db_settings.get("recommendation_calibration_horizons", "swing,positional").split(",")
            if h.strip()
        ]
        period = db_settings.get("recommendation_calibration_period", "5y") or None
        stride = int(db_settings.get("recommendation_calibration_stride", "5"))
        concurrency = int(db_settings.get("recommendation_calibration_concurrency", "3"))
        apply = str(db_settings.get("recommendation_calibration_apply", "true")).lower() in {
            "1", "true", "yes", "on",
        }

        try:
            from app.services.recommendation_tracker import evaluate_recommendation_outcomes
            await evaluate_recommendation_outcomes()
        except Exception as e:
            logger.debug("Pre-calibration recommendation evaluation skipped: %s", e)

        logger.info(
            "Weekly recommendation calibration starting: universe=%s horizons=%s period=%s stride=%s concurrency=%s apply=%s",
            universe, horizons, period, stride, concurrency, apply,
        )
        result = await run_large_scale_calibration(
            universe=universe,  # type: ignore[arg-type]
            horizons=horizons,  # type: ignore[arg-type]
            period=period,
            stride=stride,
            concurrency=concurrency,
            apply=apply,
        )
        logger.info("Weekly recommendation calibration complete: %s", result.get("summary"))

        # Auto-retrain the learnt weight vector AND the meta-labeler from
        # the freshly-evaluated outcomes. Both are no-ops below 200
        # resolved trades; once over the threshold they self-activate
        # and the engine picks them up on its next request without a
        # restart (in-memory caches updated as a side effect).
        try:
            from app.services.recommendation_tuner import logistic_fit_weights
            tune_res = await logistic_fit_weights()
            logger.info("Weekly factor-weight refit: %s", tune_res.get("status"))
        except Exception as e:
            logger.warning("Weekly factor-weight refit failed: %s", e)
        try:
            from app.services.ml_meta_label import train_meta_label_model
            meta_res = await train_meta_label_model(n_splits=5)
            logger.info(
                "Weekly meta-label refit: status=%s samples=%s cv_acc=%s",
                meta_res.get("status"), meta_res.get("samples"),
                meta_res.get("cv_accuracy_mean"),
            )
        except Exception as e:
            logger.warning("Weekly meta-label refit failed: %s", e)

    async def _auto_paper_loop(self) -> None:
        """Independent loop for the multi-factor auto-paper-trader.

        Runs every `auto_paper_interval_minutes` (default 15 min) during
        market hours. Separated from `run_scan_cycle` so the heavy
        `generate_batch` work doesn't slow the user-facing scan
        endpoint. Failures are swallowed and logged.
        """
        await asyncio.sleep(120)  # grace period after startup
        while self._running:
            try:
                if not is_market_open():
                    await asyncio.sleep(600)  # 10 min wait outside market hours
                    continue
                db_settings = await _get_settings()
                interval_min = int(db_settings.get("auto_paper_interval_minutes", 15))
                from app.services.auto_paper_trader import (
                    auto_close_hits, auto_open_from_recommendations,
                    get_auto_paper_settings,
                )
                from app.services.recommendation import generate_batch, default_universe
                cfg = await get_auto_paper_settings(db_settings)
                if cfg["enabled"]:
                    close_res = await auto_close_hits()
                    uni = [s.get("symbol") for s in (db_settings.get("watchlist") or []) if s.get("symbol")]
                    if not uni:
                        uni = default_universe(limit=40)
                    recs, _err = await generate_batch(uni, horizon="swing")
                    open_res = await auto_open_from_recommendations(
                        recs,
                        min_conviction=cfg["min_conviction"],
                        max_per_day=cfg["max_per_day"],
                        max_open_positions=cfg["max_open_positions"],
                    )
                    logger.info(
                        "Auto-paper loop: opened=%d closed=%d",
                        len(open_res.get("opened", [])),
                        len(close_res.get("closed", [])),
                    )
                await asyncio.sleep(max(60, interval_min * 60))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Auto-paper loop error: %s", e)
                await asyncio.sleep(300)

    async def _module_a_loop(self) -> None:
        """Once-per-trading-day Module A scan.

        Module A holds for 180 days, so checking once a day is plenty.
        Runs at 11:00 IST (~75 min after market open) so 52-week-low
        proximity checks against a real intraday price. Separated from
        the scan cycle so its yfinance fan-out can't slow the user UI.
        """
        await asyncio.sleep(300)
        while self._running:
            try:
                now_ist = datetime.now(IST)
                # Target 11:00 IST on a weekday — if past, target tomorrow.
                target = now_ist.replace(hour=11, minute=0, second=0, microsecond=0)
                if target <= now_ist:
                    target = target + timedelta(days=1)
                while target.weekday() >= 5:  # skip weekends
                    target = target + timedelta(days=1)
                wait_s = max(60, (target - now_ist).total_seconds())
                logger.info("Next Module A scan at %s IST (in %.1fh)", target.isoformat(), wait_s / 3600)
                await asyncio.sleep(wait_s)
                if not self._running:
                    break
                if not is_market_open():
                    # Market holiday or off-hours — skip and wait for next.
                    continue
                from app.services.module_a_live import (
                    evaluate_and_close_module_a, scan_and_open_module_a,
                )
                close_res = await evaluate_and_close_module_a()
                open_res = await scan_and_open_module_a()
                logger.info(
                    "Module A daily: opened=%d closed=%d status=%s",
                    len(open_res.get("opened", [])),
                    len(close_res.get("closed", [])),
                    open_res.get("status"),
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Module A loop error: %s", e)
                await asyncio.sleep(3600)

    async def _run_weekly_backtest(self) -> None:
        """Execute the weekly backtest and persist the result row."""
        from app.services.backtester import run_backtest
        import json
        symbols = await _get_watchlist_symbols()
        # Always include the top liquid NIFTY names so we have a stable baseline
        baseline = [s["symbol"] for s in MAJOR_STOCKS[:25]]
        all_syms = list(dict.fromkeys(baseline + symbols))[:50]  # de-dupe, cap at 50

        logger.info("Weekly autonomous backtest starting on %d symbols", len(all_syms))
        per_symbol: list[dict] = []
        all_pnl: list[float] = []
        all_wins = 0
        all_evaluated = 0
        type_pnl: dict[str, float] = {}
        type_count: dict[str, int] = {}
        type_dir_stats: dict[tuple[str, str], dict[str, float]] = {}

        # Fan out backtests with bounded concurrency. Each task is wrapped in
        # `asyncio.wait_for` so a single misbehaving symbol cannot stall the
        # whole weekly run; results are gathered with `return_exceptions=True`
        # and aggregated below.
        sem = asyncio.Semaphore(_BACKTEST_CONCURRENCY)

        async def _run_one(sym: str) -> dict:
            async with sem:
                return await asyncio.wait_for(
                    run_backtest(sym, period="1y", eval_windows=[1, 3, 5, 10]),
                    timeout=_BACKTEST_PER_SYMBOL_TIMEOUT,
                )

        tasks = [_run_one(sym) for sym in all_syms]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for sym, r in zip(all_syms, results):
            if isinstance(r, asyncio.TimeoutError):
                logger.warning(
                    "Backtest timed out after %.0fs for %s; skipping",
                    _BACKTEST_PER_SYMBOL_TIMEOUT, sym,
                )
                continue
            if isinstance(r, BaseException):
                logger.debug("Backtest failed for %s: %s", sym, r)
                continue

            per_symbol.append(r)
            ov = r.get("overall", {})
            if ov.get("avg_pnl_5d") is not None:
                all_pnl.append(ov["avg_pnl_5d"])
            # Aggregate per-type and per-(type,direction) — the latter feeds
            # the signal_edge override table that drives the live Edge/Avoid
            # badges in the extension.
            for stype, dirs in (r.get("by_signal_type") or {}).items():
                for direction, m in dirs.items():
                    if m.get("is_neutral"):
                        continue
                    wins = m.get("wins_5d", 0)
                    losses = m.get("losses_5d", 0)
                    all_wins += wins
                    all_evaluated += wins + losses
                    if m.get("avg_pnl_5d") is not None and m.get("total"):
                        type_pnl[stype] = type_pnl.get(stype, 0.0) + m["avg_pnl_5d"] * m["total"]
                        type_count[stype] = type_count.get(stype, 0) + m["total"]
                        key = (stype, direction)
                        td = type_dir_stats.setdefault(
                            key, {"pnl_sum": 0.0, "wins": 0, "losses": 0, "trades": 0}
                        )
                        td["pnl_sum"] += m["avg_pnl_5d"] * m["total"]
                        td["wins"] += wins
                        td["losses"] += losses
                        td["trades"] += m["total"]

        avg_pnl = round(sum(all_pnl) / len(all_pnl), 4) if all_pnl else 0.0
        win_rate = round(all_wins / all_evaluated * 100, 2) if all_evaluated else 0.0
        type_avg = {t: type_pnl[t] / type_count[t] for t in type_pnl if type_count[t]}
        best = max(type_avg.items(), key=lambda x: x[1])[0] if type_avg else None
        worst = min(type_avg.items(), key=lambda x: x[1])[0] if type_avg else None

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO backtest_runs
                   (run_at, period, eval_window_days, stocks_count, total_signals,
                    avg_pnl_pct, directional_win_rate, best_signal_type, worst_signal_type, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    "1y", 5, len(all_syms),
                    sum(r.get("total_signals", 0) for r in per_symbol),
                    avg_pnl, win_rate, best, worst,
                    json.dumps({"per_symbol": per_symbol, "by_type_avg_pnl_5d": type_avg}),
                ),
            )
            await db.commit()
        logger.info(
            "Weekly backtest complete: %d stocks · WR %.1f%% · avg PnL %.2f%% · best %s · worst %s",
            len(all_syms), win_rate, avg_pnl, best, worst,
        )

        # Refresh signal_edge overrides — closes the loop so the Edge/Avoid
        # badges and the recommendation engine see live numbers, not the
        # frozen baseline. Sample-size guardrail lives inside the writer.
        try:
            from app.services.signal_edge import write_edge_overrides
            edge_payload: dict[tuple[str, str], dict] = {}
            for (stype, direction), s in type_dir_stats.items():
                trades = int(s["trades"])
                if trades <= 0:
                    continue
                resolved = int(s["wins"]) + int(s["losses"])
                wr = round((s["wins"] / resolved) * 100, 2) if resolved else 0.0
                edge_payload[(stype, direction)] = {
                    "win_rate": wr,
                    "avg_pnl": round(s["pnl_sum"] / trades, 4),
                    "trades": trades,
                }
            written = await write_edge_overrides(edge_payload)
            logger.info("Edge overrides refreshed: %d/%d keys persisted", written, len(edge_payload))
        except Exception as e:
            logger.warning("Edge override refresh failed (non-critical): %s", e)

    def is_running(self) -> bool:
        return self._running


def _next_weekly_at(weekday: int, hour: int, minute: int) -> datetime:
    """Return the next IST `weekday hh:mm` slot expressed in UTC.

    weekday: Mon=0 … Sun=6 (matches `datetime.weekday()`).
    """
    IST_OFFSET = timedelta(hours=5, minutes=30)
    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc + IST_OFFSET
    days_until = (weekday - now_ist.weekday()) % 7
    target_ist = (now_ist + timedelta(days=days_until)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if days_until == 0 and now_ist >= target_ist:
        target_ist += timedelta(days=7)
    return (target_ist - IST_OFFSET).replace(tzinfo=timezone.utc)


def _next_weekly_backtest_dt() -> datetime:
    """Monday 14:00 IST — mid-session so the latest weekly bars are settled
    and the scan/auto-paper loops aren't competing for I/O during the
    morning open volatility window."""
    return _next_weekly_at(weekday=0, hour=14, minute=0)


def _next_weekly_calibration_dt() -> datetime:
    """Friday 16:30 IST."""
    return _next_weekly_at(weekday=4, hour=16, minute=30)


# Global orchestrator instance
orchestrator = SignalOrchestrator()

"""agentX FastAPI application.

NOTE: The in-process rate limiter below (`_rate_buckets`) is only correct
when the app runs as a SINGLE worker process. Each worker keeps its own
bucket dict, so with N workers the effective limit is N x configured.
For multi-worker / multi-replica deployments, replace the bucket store
with Redis (INCR + EXPIRE) before relying on these limits for abuse
control. The current limits are best-effort guardrails, not security.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import logging.handlers
import os
import secrets
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.database import init_db, migrate_plaintext_secrets
from app.services.cache import cache_manager
from app.services.orchestrator import orchestrator
from app.routers import signals, watchlist, settings as settings_router, stocks, analysis, market, performance, alerts, screener, backtest

# Newly added routers (integration sweep). Imported defensively so a
# missing/broken module from another agent doesn't take down the whole app.
try:
    from app.routers import portfolio as portfolio_router  # noqa: F401
except Exception as _portfolio_exc:  # pragma: no cover - import guard
    portfolio_router = None
    logging.getLogger(__name__).warning("portfolio router unavailable: %s", _portfolio_exc)

try:
    from app.routers import llm_usage as llm_usage_router  # noqa: F401
except Exception as _llm_usage_exc:  # pragma: no cover - import guard
    llm_usage_router = None
    logging.getLogger(__name__).warning("llm_usage router unavailable: %s", _llm_usage_exc)

try:
    from app.routers import recommendations as recommendations_router  # noqa: F401
except Exception as _rec_exc:  # pragma: no cover - import guard
    recommendations_router = None

try:
    from app.routers import stream as stream_router  # noqa: F401
except Exception as _stream_exc:  # pragma: no cover - import guard
    stream_router = None

# ── Logging setup: console + rotating file ───────────────────
_LOG_FORMAT = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

def _setup_logging() -> None:
    """Configure root logger to write to both stderr and a rotating log file."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    # Console handler (already exists from uvicorn, but ensure our format)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT))
    root.addHandler(console)

    # Rotating file handler — 5 MB per file, keep 5 backups (25 MB total max)
    log_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), settings.log_file)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT))
    root.addHandler(file_handler)

    # Quiet down noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    logging.getLogger("peewee").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
    logging.getLogger("charset_normalizer").setLevel(logging.WARNING)

_setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("StockPilot backend starting...")
    await init_db()
    # Seal any plaintext API keys / tokens still in the settings table.
    # Idempotent — already-encrypted rows are skipped. Raises
    # SecretsKeyMissing if no master key is configured outside dev mode.
    try:
        sealed = await migrate_plaintext_secrets()
        if sealed:
            logger.info("Sealed %d plaintext secret(s) on startup", sealed)
    except Exception as exc:
        logger.error("Secrets migration failed: %s", exc)
        raise
    await cache_manager.connect(settings.redis_url)

    # Seed dynamic signal weighting cache from existing performance data
    from app.services.signal_tracker import seed_performance_cache
    seeded = await seed_performance_cache()
    if seeded:
        logger.info("Dynamic signal weighting active: %d signal types loaded", seeded)

    # Seed the recommendation engine's factor-edge cache too. After the cron
    # accumulates outcomes this drives the dynamic factor weighting in
    # `recommendation._score_all`.
    from app.services.recommendation_tracker import seed_factor_edge_cache
    factors_seeded = await seed_factor_edge_cache()
    if factors_seeded:
        logger.info("Dynamic factor weighting active: %d factors loaded", factors_seeded)

    await orchestrator.start()
    logger.info("StockPilot backend ready")

    yield

    # Shutdown
    logger.info("StockPilot backend shutting down...")
    await orchestrator.stop()
    await cache_manager.disconnect()
    from app.services.nse_fetcher import shutdown_nse
    shutdown_nse()


app = FastAPI(
    title="agentX API",
    description="AI-powered trading copilot for Indian stock markets (NSE/BSE)",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — restrict to Chrome extension and localhost only
_cors_origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=r"chrome-extension://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Rate limiter (in-memory, per-IP) with periodic cleanup ---
_rate_buckets: dict[str, list[float]] = defaultdict(list)

# Ordered route-pattern table. First match wins.
# Each entry: (bucket_id, prefix, suffix_or_None, limit, window_secs)
# - prefix: path must start with this (after normalisation)
# - suffix: if not None, path must also end with it
# This avoids the previous greedy `pattern in path` bug where any path
# containing "/api/stocks/" inherited the AI-analysis limit, e.g.
# `/api/stocks/RELIANCE/technicals` was incorrectly capped at 15/min.
_ROUTE_RATE_LIMITS: list[tuple[str, str, str | None, int, int]] = [
    ("ai_analysis",  "/api/stocks/", "/ai-analysis", 15, 60),
    ("scan_trigger", "/api/scan/trigger", None,       5, 60),
    ("screener",     "/api/screener",    None,       20, 60),
    ("backtest",     "/api/backtest/",   None,       60, 60),
]
_DEFAULT_RATE_LIMIT = (120, 60)  # 120 req/min global

_request_counter = 0
_CLEANUP_EVERY_N_REQUESTS = 100

# Request timeout (seconds)
_REQUEST_TIMEOUT = 60

# Trust X-Forwarded-For only when explicitly opted in. Default: off, so
# clients can't spoof their IP by setting the header. Enable behind a
# trusted reverse proxy (nginx, ALB, Cloudflare) that overrides XFF.
_TRUST_FORWARDED = os.environ.get("TRUST_FORWARDED", "").strip() == "1"


def _cleanup_stale_buckets() -> None:
    """Remove expired entries to prevent unbounded memory growth."""
    global _request_counter
    _request_counter += 1
    if _request_counter % _CLEANUP_EVERY_N_REQUESTS != 0:
        return
    now = time.time()
    stale = [k for k, v in _rate_buckets.items() if not v or (now - max(v)) > 120]
    for k in stale:
        del _rate_buckets[k]
    if stale:
        logger.debug("Rate limiter cleanup: removed %d stale buckets", len(stale))


def _match_route_limit(path: str) -> tuple[str, int, int]:
    """Return (bucket_id, limit, window) for the given path.

    Uses ordered prefix+suffix matching so that, e.g., `/api/stocks/X/technicals`
    does NOT match the `/api/stocks/.../ai-analysis` rule.
    """
    for bucket_id, prefix, suffix, limit, window in _ROUTE_RATE_LIMITS:
        if not path.startswith(prefix):
            continue
        if suffix is not None and not path.endswith(suffix):
            continue
        return bucket_id, limit, window
    limit, window = _DEFAULT_RATE_LIMIT
    return "default", limit, window


def _is_public_ip(ip_str: str) -> bool:
    """True if `ip_str` is a routable public IP. Private/loopback/link-local fail."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _client_ip(request: Request) -> str:
    """Extract client IP. Honours X-Forwarded-For only when TRUST_FORWARDED=1
    is set, picking the leftmost public IP from the chain. Falls back to the
    direct socket peer (`request.client.host`) otherwise.
    """
    if _TRUST_FORWARDED:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            for raw in xff.split(","):
                candidate = raw.strip()
                if candidate and _is_public_ip(candidate):
                    return candidate
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def verify_api_key(provided: str | None) -> bool:
    """Validate an API key against ``settings.api_key`` using a timing-safe
    comparison. This helper is the single source of truth for API-key auth and
    is reused by non-HTTP entry points (e.g. WebSocket upgrades) that bypass
    the HTTP middleware.

    Returns ``True`` when the request should be allowed, ``False`` otherwise.

    Auth is bypassed (returns ``True``) when:
      * ``settings.api_key`` is empty (single-user / localhost mode), or
      * the ``AGENTX_DEV`` env var is set to a truthy value.
    """
    if os.environ.get("AGENTX_DEV", "").strip() in ("1", "true", "True", "yes"):
        return True
    if not settings.api_key:
        return True
    if not provided:
        return False
    return secrets.compare_digest(provided, settings.api_key)


def _check_rate_limit(ip: str, path: str) -> bool:
    """Returns True if request is allowed."""
    _cleanup_stale_buckets()
    bucket_id, limit, window = _match_route_limit(path)
    key = f"{ip}:{bucket_id}"
    now = time.time()
    _rate_buckets[key] = [t for t in _rate_buckets[key] if now - t < window]
    if len(_rate_buckets[key]) >= limit:
        return False
    _rate_buckets[key].append(now)
    return True


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next) -> Response:
    """Log every request with method, path, status, duration, and correlation ID."""
    req_id = uuid.uuid4().hex[:8]
    path = request.url.path
    method = request.method
    client_ip = _client_ip(request)
    start = time.time()

    # Skip noisy health/docs endpoints from detailed logging
    is_quiet = path in ("/api/health", "/docs", "/openapi.json", "/redoc", "/")

    if not is_quiet:
        query = str(request.url.query) if request.url.query else ""
        logger.info(
            "[%s] --> %s %s%s from %s",
            req_id, method, path, f"?{query}" if query else "", client_ip,
        )

    # Delegate to security middleware logic inline
    response = await _handle_request(request, call_next, req_id, path, client_ip)

    duration_ms = (time.time() - start) * 1000
    status = response.status_code

    if not is_quiet:
        level = logging.WARNING if status >= 400 else logging.INFO
        logger.log(
            level,
            "[%s] <-- %s %s %d (%.0fms)",
            req_id, method, path, status, duration_ms,
        )

    # Add correlation ID to response headers for client-side debugging
    response.headers["X-Request-Id"] = req_id
    return response


async def _handle_request(
    request: Request, call_next, req_id: str, path: str, client_ip: str,
) -> Response:
    """Security checks (rate limit, auth, timeout) extracted for clarity."""

    # Health and docs always allowed — skip security checks
    if path in ("/api/health", "/docs", "/openapi.json", "/redoc", "/"):
        return await call_next(request)

    # Rate limiting
    if not _check_rate_limit(client_ip, path):
        logger.warning("[%s] Rate limited: %s %s", req_id, client_ip, path)
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Try again later."},
            headers={"Retry-After": "60"},
        )

    # API key auth (timing-safe comparison via shared helper)
    if not verify_api_key(request.headers.get("X-API-Key")):
        logger.warning("[%s] Auth failed from %s", req_id, client_ip)
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing X-API-Key header"},
        )

    # Request timeout — prevent hung yfinance/LLM calls from blocking forever
    try:
        return await asyncio.wait_for(call_next(request), timeout=_REQUEST_TIMEOUT)
    except asyncio.TimeoutError:
        logger.error("[%s] TIMEOUT after %ds: %s", req_id, _REQUEST_TIMEOUT, path)
        return JSONResponse(
            status_code=504,
            content={"detail": f"Request timed out after {_REQUEST_TIMEOUT}s"},
        )


# Mount routers
app.include_router(signals.router)
app.include_router(watchlist.router)
app.include_router(settings_router.router)
app.include_router(stocks.router)
app.include_router(analysis.router)
app.include_router(market.router)
app.include_router(performance.router)
app.include_router(alerts.router)
app.include_router(screener.router)
app.include_router(backtest.router)

# Newly added routers
if portfolio_router is not None:
    app.include_router(portfolio_router.router)
if llm_usage_router is not None:
    app.include_router(llm_usage_router.router)
if recommendations_router is not None:
    # Try common naming conventions
    _r = getattr(recommendations_router, "router", None) or getattr(
        recommendations_router, "recommendations_router", None
    )
    if _r is not None:
        app.include_router(_r)
if stream_router is not None:
    _s = getattr(stream_router, "router", None) or getattr(
        stream_router, "stream_router", None
    )
    if _s is not None:
        app.include_router(_s)


@app.get("/")
async def root():
    return {"message": "StockPilot API", "version": "0.1.0", "docs": "/docs"}

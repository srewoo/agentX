"""agentX FastAPI application."""
import asyncio
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
from app.database import init_db
from app.services.cache import cache_manager
from app.services.orchestrator import orchestrator
from app.routers import signals, watchlist, settings as settings_router, stocks, analysis, market, performance, alerts, screener, backtest

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
    await cache_manager.connect(settings.redis_url)
    await orchestrator.start()
    logger.info("StockPilot backend ready")

    yield

    # Shutdown
    logger.info("StockPilot backend shutting down...")
    await orchestrator.stop()
    await cache_manager.disconnect()


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
_RATE_LIMITS = {
    "/api/stocks/{symbol}/ai-analysis": (15, 60),   # 15 req/min
    "/api/scan/trigger": (5, 60),                    # 5 req/min
    "/api/screener": (20, 60),                       # 20 req/min
    "/api/backtest/": (5, 60),                       # 5 req/min (backtests are heavy)
    "default": (120, 60),                            # 120 req/min global
}
_request_counter = 0
_CLEANUP_EVERY_N_REQUESTS = 100

# Request timeout (seconds)
_REQUEST_TIMEOUT = 60


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


def _check_rate_limit(ip: str, path: str) -> bool:
    """Returns True if request is allowed."""
    _cleanup_stale_buckets()
    # Match specific path patterns
    limit, window = _RATE_LIMITS.get("default", (60, 60))
    for pattern, (l, w) in _RATE_LIMITS.items():
        if pattern != "default" and pattern.split("{")[0] in path:
            limit, window = l, w
            break
    # Safer key extraction
    parts = path.strip("/").split("/")
    bucket_suffix = parts[1] if len(parts) > 1 else path
    key = f"{ip}:{bucket_suffix}"
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
    client_ip = request.client.host if request.client else "unknown"
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

    # API key auth (timing-safe comparison)
    if settings.api_key:
        provided = request.headers.get("X-API-Key", "")
        if not secrets.compare_digest(provided, settings.api_key):
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


@app.get("/")
async def root():
    return {"message": "StockPilot API", "version": "0.1.0", "docs": "/docs"}

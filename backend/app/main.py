"""agentX FastAPI application."""
import logging
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.database import init_db
from app.services.cache import cache_manager
from app.services.orchestrator import orchestrator
from app.routers import signals, watchlist, settings as settings_router, stocks, analysis, market, performance, alerts, screener

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
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


# --- Rate limiter (in-memory, per-IP) ---
_rate_buckets: dict[str, list[float]] = defaultdict(list)
_RATE_LIMITS = {
    "/api/stocks/{symbol}/ai-analysis": (15, 60),   # 15 req/min
    "/api/scan/trigger": (5, 60),                    # 5 req/min
    "/api/screener": (20, 60),                       # 20 req/min
    "default": (120, 60),                            # 120 req/min global
}


def _check_rate_limit(ip: str, path: str) -> bool:
    """Returns True if request is allowed."""
    # Match specific path patterns
    limit, window = _RATE_LIMITS.get("default", (60, 60))
    for pattern, (l, w) in _RATE_LIMITS.items():
        if pattern != "default" and pattern.split("{")[0] in path:
            limit, window = l, w
            break
    key = f"{ip}:{path.split('/')[2] if '/' in path else path}"
    now = time.time()
    _rate_buckets[key] = [t for t in _rate_buckets[key] if now - t < window]
    if len(_rate_buckets[key]) >= limit:
        return False
    _rate_buckets[key].append(now)
    return True


@app.middleware("http")
async def security_middleware(request: Request, call_next) -> Response:
    path = request.url.path

    # Health and docs always allowed
    if path in ("/api/health", "/docs", "/openapi.json", "/redoc", "/"):
        return await call_next(request)

    # Rate limiting
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(client_ip, path):
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Try again later."},
            headers={"Retry-After": "60"},
        )

    # API key auth (timing-safe comparison)
    if settings.api_key:
        provided = request.headers.get("X-API-Key", "")
        if not secrets.compare_digest(provided, settings.api_key):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing X-API-Key header"},
            )

    return await call_next(request)


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


@app.get("/")
async def root():
    return {"message": "StockPilot API", "version": "0.1.0", "docs": "/docs"}

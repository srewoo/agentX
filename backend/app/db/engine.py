"""SQLAlchemy 2.x engine factory — backend-agnostic persistence.

Reads `DATABASE_URL` from the environment. When unset, falls back to the
existing SQLite file path from `app.config.settings.sqlite_path`. That
default is what keeps local dev working without any change.

Why a factory and not a module-level singleton:
    Tests reconfigure the DB URL between sessions (and between
    parametrized runs in `test_db_compat`). A cached engine that points
    at a stale path is the kind of thing that pages you at 2am, so we
    cache by URL — not unconditionally.

Why both `get_engine()` and `get_session()`:
    Most of `database.py` runs raw SQL through SQLAlchemy Core (matches
    the existing dict-shaped return contract). A handful of future
    repositories may want a Session — exposing both keeps the door open
    without forcing the ORM on existing code.

Concurrency knobs:
    SQLite     -> WAL + busy_timeout=5000ms, single connection per
                  operation (StaticPool would serialize too aggressively).
    Postgres   -> pool_size = $DB_POOL_SIZE (default 10), pool_pre_ping
                  on so a recycled-by-the-load-balancer connection
                  doesn't surface as a 500 to the user.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

# Sentinel used when no DATABASE_URL is set and we have no settings module
# available (e.g. early test bootstrapping). Real default is computed in
# resolve_database_url() so it picks up settings.sqlite_path.
DEFAULT_SQLITE_URL = "sqlite:///./stockpilot.db"

# Tunables — env-overridable, never magic numbers.
_DEFAULT_POOL_SIZE = 10
_DEFAULT_MAX_OVERFLOW = 5
_SQLITE_BUSY_TIMEOUT_MS = 5000

# Cache engines per URL. A swap of DATABASE_URL between calls (tests do
# this) will produce a fresh engine — old ones are disposed lazily via
# dispose_engine().
_engine_cache: dict[str, Engine] = {}
_cache_lock = threading.Lock()


def resolve_database_url() -> str:
    """Compute the effective DB URL.

    Priority: DATABASE_URL env > settings.sqlite_path > module default.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url

    # Lazy import — tests sometimes import this module before
    # pydantic-settings can find a .env file.
    try:
        from app.config import settings

        path = settings.sqlite_path
    except Exception:  # noqa: BLE001 — defensive boundary
        return DEFAULT_SQLITE_URL

    return f"sqlite:///{path}"


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _build_engine(url: str) -> Engine:
    """Construct an Engine with backend-appropriate kwargs."""
    if _is_sqlite(url):
        # check_same_thread=False is required because FastAPI handlers
        # may touch the engine from worker threads (e.g. for sync
        # endpoints that delegate to a thread pool). The actual
        # serialization is handled by SQLite's own locking + WAL.
        engine = create_engine(
            url,
            future=True,
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )

        # Per-connection PRAGMAs — busy_timeout / foreign_keys do not
        # persist at the DB level, so we apply them on every checkout.
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _conn_record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
                cur.execute("PRAGMA foreign_keys=ON")
            finally:
                cur.close()

        return engine

    # Postgres (or anything else) — use a real pool.
    pool_size = int(os.environ.get("DB_POOL_SIZE", _DEFAULT_POOL_SIZE))
    max_overflow = int(os.environ.get("DB_MAX_OVERFLOW", _DEFAULT_MAX_OVERFLOW))
    return create_engine(
        url,
        future=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
    )


def get_engine(url: Optional[str] = None) -> Engine:
    """Return (and cache) the Engine for the given URL.

    Passing `url=None` resolves the active URL via env / settings. Tests
    can pass an explicit URL to avoid touching globals.
    """
    effective = url or resolve_database_url()
    with _cache_lock:
        eng = _engine_cache.get(effective)
        if eng is None:
            eng = _build_engine(effective)
            _engine_cache[effective] = eng
            logger.info(
                "db.engine created url=%s dialect=%s",
                _redact(effective),
                eng.dialect.name,
            )
        return eng


def dispose_engine(url: Optional[str] = None) -> None:
    """Dispose and forget a cached engine (used by tests)."""
    effective = url or resolve_database_url()
    with _cache_lock:
        eng = _engine_cache.pop(effective, None)
    if eng is not None:
        eng.dispose()


def get_session(url: Optional[str] = None) -> Session:
    """Return a fresh ORM session bound to the active engine.

    Caller is responsible for closing it — preferably via
    `with get_session() as s: ...`.
    """
    eng = get_engine(url)
    factory = sessionmaker(bind=eng, expire_on_commit=False, future=True)
    return factory()


def get_dialect(url: Optional[str] = None) -> str:
    """Return the SQLAlchemy dialect name (`sqlite`, `postgresql`, ...)."""
    return get_engine(url).dialect.name


def apply_migration_sql(sql: str, url: Optional[str] = None) -> None:
    """Run a multi-statement SQL script in the active backend.

    SQLite's DB-API supports `executescript`; Postgres does not. We split
    on semicolons (after stripping `--` line comments) and execute each
    statement individually inside a single transaction. Statements
    consisting only of whitespace are skipped.

    This keeps `services.portfolio.ensure_schema()` working unchanged on
    SQLite and correct on Postgres.
    """
    statements = _split_sql(sql)
    if not statements:
        return
    eng = get_engine(url)
    with eng.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))


def _split_sql(sql: str) -> list[str]:
    """Best-effort SQL statement splitter.

    Strips line comments (`-- ...`) and splits on `;`. Does NOT handle
    semicolons inside string literals — fine for our migration files,
    which don't contain any. If that ever changes, swap this for
    `sqlparse.split()`.
    """
    cleaned_lines: list[str] = []
    for line in sql.splitlines():
        idx = line.find("--")
        cleaned_lines.append(line[:idx] if idx >= 0 else line)
    cleaned = "\n".join(cleaned_lines)
    return [s.strip() for s in cleaned.split(";") if s.strip()]


def _redact(url: str) -> str:
    """Hide the password component of a DB URL for log lines."""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        creds = f"{user}:***"
    return f"{scheme}://{creds}@{host}"

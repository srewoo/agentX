"""Database setup.

Historically a thin aiosqlite wrapper. Now driver-agnostic: when
`DATABASE_URL` points at a non-SQLite backend (typically Postgres in
prod), the helpers in this module route through the SQLAlchemy engine
in `app.db.engine`. When unset (or pointing at SQLite), the legacy
aiosqlite hot path is preserved verbatim — local dev keeps running with
zero changes.

Public surface (kept stable for back-compat):
    DB_PATH, init_db, cleanup_old_signals, get_db, connect,
    record_llm_usage, get_today_llm_spend_usd

New surface:
    apply_migration_sql(sql) — driver-aware multi-statement runner.
        Use this from places that previously called
        `aiosqlite.executescript(...)` (e.g. portfolio.ensure_schema).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiosqlite
from app.config import settings
from app.db.engine import (
    apply_migration_sql as _engine_apply_migration_sql,
    get_engine,
    resolve_database_url,
)
from sqlalchemy import text

logger = logging.getLogger(__name__)

DB_PATH = settings.sqlite_path

CREATE_SIGNALS_TABLE = """
CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    direction TEXT NOT NULL,
    strength INTEGER NOT NULL,
    reason TEXT NOT NULL,
    risk TEXT,
    llm_summary TEXT,
    current_price REAL,
    metadata TEXT,
    created_at TEXT NOT NULL,
    read INTEGER DEFAULT 0,
    dismissed INTEGER DEFAULT 0
);
"""

CREATE_WATCHLIST_TABLE = """
CREATE TABLE IF NOT EXISTS watchlist (
    symbol TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    exchange TEXT DEFAULT 'NSE',
    added_at TEXT NOT NULL
);
"""

CREATE_SETTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

CREATE_SIGNAL_OUTCOMES_TABLE = """
CREATE TABLE IF NOT EXISTS signal_outcomes (
    signal_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    entry_time TEXT NOT NULL,
    exit_time TEXT,
    pnl_pct REAL,
    outcome TEXT,
    hold_days INTEGER,
    evaluated_at TEXT
);
"""

CREATE_PRICE_ALERTS_TABLE = """
CREATE TABLE IF NOT EXISTS price_alerts (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    target_price REAL NOT NULL,
    condition TEXT NOT NULL,
    current_price_at_creation REAL,
    created_at TEXT NOT NULL,
    triggered_at TEXT,
    triggered_price REAL,
    active INTEGER DEFAULT 1,
    note TEXT,
    pct_threshold REAL
);
"""

CREATE_PRICE_ALERTS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_price_alerts_active ON price_alerts(active);",
    "CREATE INDEX IF NOT EXISTS idx_price_alerts_symbol ON price_alerts(symbol);",
]

CREATE_SIGNAL_PERFORMANCE_TABLE = """
CREATE TABLE IF NOT EXISTS signal_performance (
    signal_type TEXT NOT NULL,
    direction TEXT NOT NULL,
    timeframe TEXT NOT NULL DEFAULT 'all',
    total_signals INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    avg_pnl_pct REAL DEFAULT 0,
    win_rate REAL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (signal_type, direction, timeframe)
);
"""

CREATE_SIGNALS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_signals_unread ON signals(read, dismissed);",
]

# `INTEGER PRIMARY KEY AUTOINCREMENT` is SQLite-specific. We render the right
# token per dialect so the same DDL string works on Postgres too.
_AUTOINC_PK_BY_DIALECT = {
    "sqlite": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "postgresql": "BIGSERIAL PRIMARY KEY",
}


def _autoinc_pk(dialect: str) -> str:
    return _AUTOINC_PK_BY_DIALECT.get(dialect, "BIGINT PRIMARY KEY")


def _create_backtest_runs_sql(dialect: str) -> str:
    return f"""
    CREATE TABLE IF NOT EXISTS backtest_runs (
        id {_autoinc_pk(dialect)},
        run_at TEXT NOT NULL,
        period TEXT NOT NULL,
        eval_window_days INTEGER NOT NULL,
        stocks_count INTEGER NOT NULL,
        total_signals INTEGER NOT NULL,
        avg_pnl_pct REAL,
        directional_win_rate REAL,
        best_signal_type TEXT,
        worst_signal_type TEXT,
        payload TEXT NOT NULL
    );
    """


def _create_llm_usage_sql(dialect: str) -> str:
    return f"""
    CREATE TABLE IF NOT EXISTS llm_usage (
        id {_autoinc_pk(dialect)},
        ts TEXT NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        request_id TEXT,
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        cost_inr REAL NOT NULL DEFAULT 0,
        route TEXT,
        symbol TEXT,
        success INTEGER NOT NULL DEFAULT 1
    );
    """


# Backwards-compat constants — preserved so any external import still works.
CREATE_RECOMMENDATION_OUTCOMES_TABLE = """
CREATE TABLE IF NOT EXISTS recommendation_outcomes (
    rec_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    horizon TEXT NOT NULL,
    action TEXT NOT NULL,           -- BUY / SELL (HOLD/AVOID never tracked)
    conviction INTEGER NOT NULL,
    entry REAL NOT NULL,
    stoploss REAL NOT NULL,
    target1 REAL NOT NULL,
    timeframe_days INTEGER NOT NULL,
    -- JSON-encoded signal contributions ({name, score, weight, ...}).
    -- Persisting the contributions lets us compute factor edge later
    -- without re-running the engine.
    signals_json TEXT NOT NULL,
    sector TEXT,
    created_at TEXT NOT NULL,
    -- Outcome (filled by evaluate_recommendation_outcomes cron):
    outcome TEXT,                   -- 'win' | 'loss' | 'expired' | NULL
    exit_price REAL,
    exit_time TEXT,
    pnl_pct REAL,
    evaluated_at TEXT
);
"""

CREATE_FACTOR_PERFORMANCE_TABLE = """
CREATE TABLE IF NOT EXISTS factor_performance (
    factor TEXT PRIMARY KEY,        -- 'trend' | 'momentum' | ... matches SignalContribution.name
    -- Edge = mean P&L on recs where the factor's score was >0.3 in the
    -- direction of the action, vs all directional recs. Updated after
    -- every _recalculate_factor_performance() pass.
    total_directional INTEGER DEFAULT 0,
    aligned_count INTEGER DEFAULT 0,
    aligned_avg_pnl REAL DEFAULT 0,
    overall_avg_pnl REAL DEFAULT 0,
    edge REAL DEFAULT 0,            -- aligned_avg_pnl - overall_avg_pnl
    updated_at TEXT
);
"""

CREATE_BACKTEST_RUNS_TABLE = _create_backtest_runs_sql("sqlite")
CREATE_BACKTEST_RUNS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_backtest_runs_run_at ON backtest_runs(run_at DESC);",
]
CREATE_LLM_USAGE_TABLE = _create_llm_usage_sql("sqlite")
CREATE_LLM_USAGE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts DESC);",
    "CREATE INDEX IF NOT EXISTS idx_llm_usage_provider_ts ON llm_usage(provider, ts DESC);",
]

DEFAULT_SETTINGS = {
    "alert_interval_minutes": str(settings.default_alert_interval_minutes),
    "risk_mode": "balanced",
    "signal_types": '["intraday","swing","long_term"]',
    "llm_provider": settings.default_llm_provider,
    "llm_model": settings.default_llm_model,
    "llm_api_key": "",
    "openai_api_key": settings.openai_api_key,
    "gemini_api_key": settings.gemini_api_key,
    "claude_api_key": settings.claude_api_key,
    # Configurable signal thresholds (can be tuned per user's strategy)
    "rsi_overbought": "70",
    "rsi_oversold": "30",
    "price_spike_pct": "3.0",
    "volume_spike_ratio": "2.0",
    "breakout_min_score": "4",
}

# Max age for signals before archival
_SIGNAL_MAX_AGE_DAYS = 7


def _using_sqlite() -> bool:
    """Active backend is SQLite (default)?

    Safe to call cheaply — engine lookup is cached.
    """
    url = resolve_database_url()
    return url.startswith("sqlite")


# ─────────────────────────────────────────────
# Schema bootstrap
# ─────────────────────────────────────────────

async def init_db() -> None:
    """Initialize database schema and default settings.

    On SQLite, keeps the original aiosqlite path (preserves WAL setup
    and avoids breaking existing local dev).

    On Postgres (or anything non-SQLite), runs the same DDL through
    SQLAlchemy. Identical end-state, dialect-appropriate types.
    """
    if _using_sqlite():
        await _init_db_sqlite()
    else:
        # Run blocking SQLAlchemy work in a worker thread — don't pin
        # the event loop while DDL is running.
        await asyncio.to_thread(_init_db_sqlalchemy)
    logger.info("Database initialized url=%s", _safe_url())


def _active_sqlite_path() -> str:
    """Resolve the on-disk path the active sqlite URL points at.

    `DATABASE_URL=sqlite:///./foo.db` -> `./foo.db`. Falls back to the
    process-wide `DB_PATH` (settings.sqlite_path) if no env override.
    """
    url = resolve_database_url()
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    return DB_PATH


async def _init_db_sqlite() -> None:
    async with aiosqlite.connect(_active_sqlite_path()) as db:
        # Concurrency / durability tuning. WAL is persistent (database-level);
        # synchronous=NORMAL is also persistent — both safe and considerably
        # faster than the rollback-journal default. busy_timeout is per-connection
        # only, so we re-apply it everywhere we open a connection.
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute("PRAGMA busy_timeout=5000;")  # 5s wait before "database is locked"
        await db.execute("PRAGMA temp_store=MEMORY;")
        await db.execute("PRAGMA foreign_keys=ON;")

        await db.execute(CREATE_SIGNALS_TABLE)
        await db.execute(CREATE_WATCHLIST_TABLE)
        await db.execute(CREATE_SETTINGS_TABLE)
        await db.execute(CREATE_SIGNAL_OUTCOMES_TABLE)
        await db.execute(CREATE_SIGNAL_PERFORMANCE_TABLE)
        await db.execute(CREATE_PRICE_ALERTS_TABLE)
        await db.execute(CREATE_RECOMMENDATION_OUTCOMES_TABLE)
        await db.execute(CREATE_FACTOR_PERFORMANCE_TABLE)
        await db.execute(_create_backtest_runs_sql("sqlite"))
        await db.execute(_create_llm_usage_sql("sqlite"))

        for idx_sql in CREATE_SIGNALS_INDEXES:
            await db.execute(idx_sql)
        for idx_sql in CREATE_PRICE_ALERTS_INDEXES:
            await db.execute(idx_sql)
        for idx_sql in CREATE_BACKTEST_RUNS_INDEXES:
            await db.execute(idx_sql)
        for idx_sql in CREATE_LLM_USAGE_INDEXES:
            await db.execute(idx_sql)

        # Seed default settings (INSERT OR IGNORE — don't overwrite user changes)
        for key, value in DEFAULT_SETTINGS.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )

        await db.commit()


def _init_db_sqlalchemy() -> None:
    """Sync DDL path for non-SQLite backends. Idempotent."""
    eng = get_engine()
    dialect = eng.dialect.name
    statements: list[str] = [
        CREATE_SIGNALS_TABLE,
        CREATE_WATCHLIST_TABLE,
        CREATE_SETTINGS_TABLE,
        CREATE_SIGNAL_OUTCOMES_TABLE,
        CREATE_SIGNAL_PERFORMANCE_TABLE,
        CREATE_PRICE_ALERTS_TABLE,
        _create_backtest_runs_sql(dialect),
        _create_llm_usage_sql(dialect),
        *CREATE_SIGNALS_INDEXES,
        *CREATE_PRICE_ALERTS_INDEXES,
        *CREATE_BACKTEST_RUNS_INDEXES,
        *CREATE_LLM_USAGE_INDEXES,
    ]
    with eng.begin() as conn:
        for stmt in statements:
            stripped = stmt.strip().rstrip(";").strip()
            if stripped:
                conn.execute(text(stripped))

        # ON CONFLICT DO NOTHING is supported by both SQLite (3.24+) and
        # Postgres (9.5+). Replaces SQLite's `INSERT OR IGNORE`.
        upsert_sql = (
            "INSERT INTO settings (key, value) VALUES (:key, :value) "
            "ON CONFLICT (key) DO NOTHING"
        )
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(text(upsert_sql), {"key": key, "value": value})


# ─────────────────────────────────────────────
# Maintenance
# ─────────────────────────────────────────────

async def cleanup_old_signals() -> int:
    """Archive signals older than _SIGNAL_MAX_AGE_DAYS.

    Deletes read+dismissed signals; keeps unread ones regardless of age.
    Returns number of rows deleted.
    """
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_SIGNAL_MAX_AGE_DAYS)).isoformat()

    if _using_sqlite():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "DELETE FROM signals WHERE created_at < ? AND read = 1 AND dismissed = 1",
                (cutoff,),
            )
            deleted = cursor.rowcount
            # Also vacuum periodically to reclaim space
            if deleted > 50:
                await db.execute("PRAGMA incremental_vacuum(50);")
            await db.commit()
    else:
        deleted = await asyncio.to_thread(_cleanup_old_signals_sync, cutoff)

    if deleted > 0:
        logger.info(
            "Signal cleanup: removed %d old signals (older than %d days)",
            deleted,
            _SIGNAL_MAX_AGE_DAYS,
        )
    return deleted


def _cleanup_old_signals_sync(cutoff: str) -> int:
    eng = get_engine()
    with eng.begin() as conn:
        result = conn.execute(
            text(
                "DELETE FROM signals WHERE created_at < :cutoff "
                "AND read = 1 AND dismissed = 1"
            ),
            {"cutoff": cutoff},
        )
        return int(result.rowcount or 0)


# ─────────────────────────────────────────────
# Connection helpers (legacy aiosqlite shape, preserved verbatim)
# ─────────────────────────────────────────────

async def _apply_per_conn_pragmas(conn: aiosqlite.Connection) -> None:
    """Apply per-connection pragmas. WAL + synchronous persist at the DB level
    via init_db(); busy_timeout and foreign_keys are per-connection."""
    await conn.execute("PRAGMA busy_timeout=5000;")
    await conn.execute("PRAGMA foreign_keys=ON;")


async def get_db() -> aiosqlite.Connection:
    """Return a connected aiosqlite connection. Caller must close.

    SQLite-only. Callers using this against a Postgres backend will fail
    fast — that's intentional. The migration runbook documents which
    consumer files need to switch to the SQLAlchemy session helpers.
    """
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await _apply_per_conn_pragmas(conn)
    return conn


def connect():
    """Async context-manager wrapper around aiosqlite.connect that auto-applies
    per-connection pragmas (busy_timeout, foreign_keys). Drop-in replacement
    for `async with aiosqlite.connect(DB_PATH) as db:` in hot paths."""
    return _ConnectCM()


class _ConnectCM:
    async def __aenter__(self):
        self._conn = await aiosqlite.connect(DB_PATH)
        await _apply_per_conn_pragmas(self._conn)
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        await self._conn.close()


# ─────────────────────────────────────────────
# Driver-aware migration runner
# ─────────────────────────────────────────────

async def apply_migration_sql(sql: str) -> None:
    """Apply a multi-statement SQL script against the active backend.

    Replaces ad-hoc `aiosqlite.executescript(...)` calls in services so a
    Postgres switch doesn't require touching them. On SQLite we use
    `executescript` for fidelity; on other backends we delegate to the
    SQLAlchemy splitter in `app.db.engine`.
    """
    if _using_sqlite():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executescript(sql)
            await db.commit()
        return
    await asyncio.to_thread(_engine_apply_migration_sql, sql)


# ─────────────────────────────────────────────
# LLM usage accounting
# ─────────────────────────────────────────────

async def record_llm_usage(
    *,
    provider: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_usd: float = 0.0,
    cost_inr: float = 0.0,
    request_id: Optional[str] = None,
    route: Optional[str] = None,
    symbol: Optional[str] = None,
    success: bool = True,
    ts: Optional[str] = None,
) -> None:
    """Persist a single LLM call's usage + cost.

    Best-effort: never raises. Failures are logged at WARNING. Caller
    should treat this as fire-and-forget observability — request handling
    must not depend on this succeeding.
    """
    from datetime import datetime, timezone

    if ts is None:
        ts = datetime.now(timezone.utc).isoformat()

    try:
        if _using_sqlite():
            async with connect() as db:
                await db.execute(
                    """
                    INSERT INTO llm_usage
                      (ts, provider, model, request_id, prompt_tokens,
                       completion_tokens, cost_usd, cost_inr, route, symbol, success)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts,
                        provider,
                        model,
                        request_id,
                        int(prompt_tokens or 0),
                        int(completion_tokens or 0),
                        float(cost_usd or 0.0),
                        float(cost_inr or 0.0),
                        route,
                        symbol,
                        1 if success else 0,
                    ),
                )
                await db.commit()
        else:
            await asyncio.to_thread(
                _record_llm_usage_sync,
                ts,
                provider,
                model,
                request_id,
                int(prompt_tokens or 0),
                int(completion_tokens or 0),
                float(cost_usd or 0.0),
                float(cost_inr or 0.0),
                route,
                symbol,
                1 if success else 0,
            )
    except Exception as exc:  # pragma: no cover — observability path
        logger.warning("record_llm_usage failed: %s", exc)


def _record_llm_usage_sync(
    ts: str,
    provider: str,
    model: str,
    request_id: Optional[str],
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    cost_inr: float,
    route: Optional[str],
    symbol: Optional[str],
    success: int,
) -> None:
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO llm_usage
                  (ts, provider, model, request_id, prompt_tokens,
                   completion_tokens, cost_usd, cost_inr, route, symbol, success)
                VALUES (:ts, :provider, :model, :request_id, :prompt_tokens,
                        :completion_tokens, :cost_usd, :cost_inr, :route, :symbol, :success)
                """
            ),
            {
                "ts": ts,
                "provider": provider,
                "model": model,
                "request_id": request_id,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": cost_usd,
                "cost_inr": cost_inr,
                "route": route,
                "symbol": symbol,
                "success": success,
            },
        )


async def get_today_llm_spend_usd() -> float:
    """Return total cost_usd for today (UTC). 0.0 on any error."""
    from datetime import datetime, timezone

    day_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    try:
        if _using_sqlite():
            async with connect() as db:
                cursor = await db.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE ts >= ?",
                    (day_start,),
                )
                row = await cursor.fetchone()
                return float(row[0]) if row and row[0] is not None else 0.0
        return await asyncio.to_thread(_get_today_llm_spend_sync, day_start)
    except Exception as exc:
        logger.warning("get_today_llm_spend_usd failed: %s", exc)
        return 0.0


def _get_today_llm_spend_sync(day_start: str) -> float:
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(
            text(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE ts >= :ts"
            ),
            {"ts": day_start},
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0


# ─────────────────────────────────────────────
# Settings KV access (with transparent at-rest decryption)
# ─────────────────────────────────────────────
#
# These helpers are the canonical surface for reading single rows out of
# the `settings` table. They sit on top of the secrets layer (ADR-003) so
# consumers never have to know whether a value is sealed:
#
#   * `get_setting_raw(key)`  → exact column value (ciphertext if sealed).
#   * `get_setting(key)`      → plaintext for SECRET_KEYS, raw for others.
#   * `get_setting_sync(key)` → sync shim for legacy call sites; only safe
#                                to call from threads with no running loop.
#
# Bulk readers (router GETs, channel config builders) should keep using
# their existing `SELECT key, value FROM settings` query but post-process
# SECRET_KEYS values through `_decrypt_settings_map()` below — that keeps
# the "one round-trip per page-load" property while still decrypting.


async def get_setting_raw(key: str) -> Optional[str]:
    """Return the exact stored value for `key` (no decryption). None if missing."""
    if _using_sqlite():
        async with connect() as db:
            cursor = await db.execute(
                "SELECT value FROM settings WHERE key = ? LIMIT 1",
                (key,),
            )
            row = await cursor.fetchone()
            return row[0] if row else None
    return await asyncio.to_thread(_get_setting_raw_sync, key)


def _get_setting_raw_sync(key: str) -> Optional[str]:
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(
            text("SELECT value FROM settings WHERE key = :key"),
            {"key": key},
        ).fetchone()
        return row[0] if row else None


async def get_setting(key: str) -> Optional[str]:
    """Return the plaintext value for `key`.

    For keys in `SECRET_KEYS`, transparently unseals the stored ciphertext.
    For all other keys, returns the raw stored string. Returns None when
    the row does not exist.

    Failure modes worth knowing:
    - If a SECRET_KEYS row is somehow stored as plaintext (e.g. a manual
      INSERT before the seal-on-write path landed), it passes through.
    - If decryption fails (wrong master key, tampered token), the
      underlying ValueError is re-raised — we want the operator to see
      that, not silently fall back to ciphertext.
    """
    raw = await get_setting_raw(key)
    if raw is None:
        return None
    # Local import — avoids a cold-import cycle since secrets.py itself
    # has no DB dependency, but keeps `database.py` import time light.
    from app.services.secrets import SECRET_KEYS, get_manager

    if key in SECRET_KEYS:
        return get_manager().unseal_key(raw)
    return raw


def get_setting_sync(key: str) -> Optional[str]:
    """Sync wrapper around :func:`get_setting`.

    Use only from sync call sites that have no running event loop (e.g. a
    background thread, a Click CLI). If a loop *is* running on the current
    thread we raise — bridging into asyncio from inside a coroutine via
    `asyncio.run` would deadlock or worse.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        raise RuntimeError(
            "get_setting_sync() called from a running event loop — "
            "use `await get_setting(key)` instead."
        )
    return asyncio.run(get_setting(key))


def _decrypt_settings_map(rows: dict[str, str]) -> dict[str, str]:
    """Return a copy of `rows` with SECRET_KEYS values unsealed in-place.

    Cheap to call on the full settings dump — non-secret keys pass through
    untouched. Used by bulk readers (analysis router, notifications
    provider config) that previously did a raw `SELECT key, value`.
    """
    from app.services.secrets import SECRET_KEYS, get_manager

    if not rows:
        return rows
    mgr = None
    out: dict[str, str] = {}
    for k, v in rows.items():
        if k in SECRET_KEYS and isinstance(v, str) and v:
            if mgr is None:
                mgr = get_manager()
            try:
                out[k] = mgr.unseal_key(v)
            except ValueError as exc:
                # A tampered or wrong-key value should fail loudly —
                # silently leaking ciphertext to a downstream consumer
                # would manifest as opaque "auth failed" errors at the
                # external API. Better to surface here.
                logger.error(
                    "Failed to decrypt settings key=%s: %s", k, exc
                )
                raise
        else:
            out[k] = v
    return out


async def migrate_plaintext_secrets() -> int:
    """Seal any SECRET_KEYS rows still stored as plaintext. Returns the
    number of rows updated.

    Idempotent: already-sealed rows are detected by their `enc:v1:` prefix
    and skipped. Empty values are also skipped — the seal-on-write path
    treats `""` as a sentinel for "not configured" and persists it
    verbatim, so we keep that contract here.
    """
    from app.services.secrets import get_manager

    mgr = get_manager()

    if _using_sqlite():
        async with connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT key, value FROM settings") as cur:
                rows = await cur.fetchall()
            updates = mgr.migrate_plaintext(
                ((r["key"], r["value"]) for r in rows)
            )
            for k, sealed in updates:
                await db.execute(
                    "UPDATE settings SET value = ? WHERE key = ?",
                    (sealed, k),
                )
            if updates:
                await db.commit()
            return len(updates)

    return await asyncio.to_thread(_migrate_plaintext_secrets_sync)


def _migrate_plaintext_secrets_sync() -> int:
    from app.services.secrets import get_manager

    mgr = get_manager()
    eng = get_engine()
    with eng.begin() as conn:
        rows = conn.execute(text("SELECT key, value FROM settings")).fetchall()
        updates = mgr.migrate_plaintext(((r[0], r[1]) for r in rows))
        for k, sealed in updates:
            conn.execute(
                text("UPDATE settings SET value = :v WHERE key = :k"),
                {"v": sealed, "k": k},
            )
        return len(updates)


def _safe_url() -> str:
    """Return active DB URL with credentials redacted — for log lines."""
    url = resolve_database_url()
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        creds = f"{user}:***"
    return f"{scheme}://{creds}@{host}"

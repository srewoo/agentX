"""Driver compatibility tests for the SQLAlchemy persistence layer.

These tests are parametrized over backends:

    * `sqlite:///<tmp>` — always runs.
    * `$TEST_POSTGRES_URL`  — runs only if the env var is set AND the
      `psycopg` driver is importable AND the URL accepts a connection.
      We skip cleanly otherwise — CI without Docker stays green.

The tests exist to catch regressions in three places that historically
break across dialects:

    1. DDL portability (BIGSERIAL vs INTEGER PRIMARY KEY AUTOINCREMENT).
    2. Parameter style (`?` vs `:name`) — we normalize via SQLAlchemy
       text() so callers don't care.
    3. Concurrency: SQLite must not deadlock under bounded write
       concurrency; Postgres must not throw "database is locked".
"""

from __future__ import annotations

import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.db.engine import (
    apply_migration_sql,
    dispose_engine,
    get_engine,
    resolve_database_url,
)


# ── Backend discovery ─────────────────────────────────────────

def _sqlite_url() -> str:
    fd, path = tempfile.mkstemp(prefix="agentx_compat_", suffix=".db")
    os.close(fd)
    # Caller is responsible for cleanup; tests are short-lived.
    return f"sqlite:///{path}"


def _postgres_url_or_none() -> str | None:
    """Return $TEST_POSTGRES_URL only if it's reachable, else None.

    We refuse to attempt a connection in module scope (test collection
    must not block on a slow remote DB), so the connectivity probe runs
    once and is cached.
    """
    url = os.environ.get("TEST_POSTGRES_URL", "").strip()
    if not url:
        return None
    try:
        import psycopg  # noqa: F401  — driver presence check
    except ImportError:
        return None
    # Probe: open + close a connection. If it fails, skip (don't error).
    try:
        eng = get_engine(url)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError:
        return None
    except Exception:
        return None
    finally:
        # Don't pollute the cache — the parametrized fixture re-creates.
        dispose_engine(url)
    return url


_BACKENDS: list = [pytest.param(_sqlite_url(), id="sqlite")]
_pg_url = _postgres_url_or_none()
if _pg_url:
    _BACKENDS.append(pytest.param(_pg_url, id="postgres"))
else:
    _BACKENDS.append(
        pytest.param(
            "postgresql+psycopg://unreachable/none",
            id="postgres",
            marks=pytest.mark.skip(
                reason="TEST_POSTGRES_URL not set / psycopg missing / DB unreachable"
            ),
        )
    )


# ── Fixtures ──────────────────────────────────────────────────

@pytest.fixture(params=_BACKENDS)
def db_url(request, monkeypatch) -> str:
    """Parametrized DB URL. Sets DATABASE_URL for the duration of the test."""
    url = request.param
    monkeypatch.setenv("DATABASE_URL", url)
    yield url
    dispose_engine(url)


@pytest.fixture
def fresh_schema(db_url: str) -> str:
    """Initialize the canonical schema on the active backend, return URL."""
    # Re-import inside the fixture so the module-level DB_PATH evaluation
    # has already happened against any process-wide default.
    from app import database as db_module

    # init_db is async; run via asyncio.
    import asyncio

    asyncio.run(db_module.init_db())
    return db_url


# ── Tests ─────────────────────────────────────────────────────

def test_resolve_database_url_picks_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    assert resolve_database_url() == "postgresql+psycopg://u:p@h/db"


def test_resolve_database_url_falls_back(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    url = resolve_database_url()
    assert url.startswith("sqlite")


def test_schema_creates_cleanly(fresh_schema: str):
    """All canonical tables exist after init_db() on this backend."""
    eng = get_engine(fresh_schema)
    expected = {
        "signals",
        "watchlist",
        "settings",
        "signal_outcomes",
        "signal_performance",
        "price_alerts",
        "backtest_runs",
        "llm_usage",
    }
    with eng.connect() as conn:
        if eng.dialect.name == "sqlite":
            rows = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        else:
            rows = conn.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                )
            ).fetchall()
        present = {r[0] for r in rows}
    missing = expected - present
    assert not missing, f"missing tables on {eng.dialect.name}: {missing}"


def test_settings_crud(fresh_schema: str):
    """Round-trip a settings row via the SQLAlchemy layer."""
    eng = get_engine(fresh_schema)
    key = f"compat_test_{uuid.uuid4().hex[:8]}"
    with eng.begin() as conn:
        conn.execute(
            text("INSERT INTO settings (key, value) VALUES (:k, :v)"),
            {"k": key, "v": "hello"},
        )
    with eng.connect() as conn:
        row = conn.execute(
            text("SELECT value FROM settings WHERE key = :k"), {"k": key}
        ).fetchone()
    assert row is not None
    assert row[0] == "hello"


def test_signal_insert_and_count(fresh_schema: str):
    eng = get_engine(fresh_schema)
    sig_id = uuid.uuid4().hex
    with eng.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO signals
                  (id, symbol, signal_type, direction, strength, reason, created_at)
                VALUES (:id, :sym, :st, :dir, :str, :rsn, :ts)
                """
            ),
            {
                "id": sig_id,
                "sym": "RELIANCE",
                "st": "price_spike",
                "dir": "bullish",
                "str": 7,
                "rsn": "compat test",
                "ts": datetime.now(timezone.utc).isoformat(),
            },
        )
    with eng.connect() as conn:
        row = conn.execute(
            text("SELECT symbol, strength FROM signals WHERE id = :id"),
            {"id": sig_id},
        ).fetchone()
    assert row == ("RELIANCE", 7)


def test_transaction_rollback_on_error(fresh_schema: str):
    """A SQL error inside `eng.begin()` rolls back — no partial writes."""
    eng = get_engine(fresh_schema)
    key = f"rollback_{uuid.uuid4().hex[:8]}"
    with pytest.raises(Exception):
        with eng.begin() as conn:
            conn.execute(
                text("INSERT INTO settings (key, value) VALUES (:k, :v)"),
                {"k": key, "v": "first"},
            )
            # Force an error — duplicate PK on second insert.
            conn.execute(
                text("INSERT INTO settings (key, value) VALUES (:k, :v)"),
                {"k": key, "v": "second"},
            )

    with eng.connect() as conn:
        row = conn.execute(
            text("SELECT value FROM settings WHERE key = :k"), {"k": key}
        ).fetchone()
    assert row is None, "rollback should have removed the first insert"


def test_apply_migration_sql_multi_statement(fresh_schema: str):
    """The dialect-aware splitter handles multi-statement scripts."""
    table = f"compat_tmp_{uuid.uuid4().hex[:8]}"
    sql = f"""
    -- comment line
    CREATE TABLE IF NOT EXISTS {table} (
        id TEXT PRIMARY KEY,
        v INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_{table}_v ON {table}(v);
    """
    apply_migration_sql(sql)
    eng = get_engine(fresh_schema)
    with eng.begin() as conn:
        conn.execute(text(f"INSERT INTO {table} (id, v) VALUES ('a', 1)"))
        conn.execute(text(f"INSERT INTO {table} (id, v) VALUES ('b', 2)"))
    with eng.connect() as conn:
        row = conn.execute(
            text(f"SELECT COUNT(*) FROM {table}")
        ).fetchone()
    assert row[0] == 2

    # Cleanup so the next parametrized backend starts fresh.
    with eng.begin() as conn:
        conn.execute(text(f"DROP TABLE {table}"))


def test_concurrent_writes_no_deadlock(fresh_schema: str):
    """5 threads inserting in parallel — must not deadlock or lose rows."""
    eng = get_engine(fresh_schema)
    n_threads = 5
    inserts_per_thread = 4
    errors: list[BaseException] = []
    written_ids: list[str] = []
    lock = threading.Lock()

    def worker(tid: int) -> None:
        try:
            for i in range(inserts_per_thread):
                sig_id = f"thr-{tid}-{i}-{uuid.uuid4().hex[:6]}"
                with eng.begin() as conn:
                    conn.execute(
                        text(
                            """
                            INSERT INTO signals
                              (id, symbol, signal_type, direction, strength, reason, created_at)
                            VALUES (:id, 'X', 'compat', 'bullish', 1, 'concurrent', :ts)
                            """
                        ),
                        {"id": sig_id, "ts": datetime.now(timezone.utc).isoformat()},
                    )
                with lock:
                    written_ids.append(sig_id)
        except BaseException as exc:  # noqa: BLE001 — capture for assert below
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "thread hung — possible deadlock"

    assert not errors, f"concurrent writes raised: {errors!r}"
    assert len(written_ids) == n_threads * inserts_per_thread

    with eng.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM signals WHERE symbol = 'X'")
        ).fetchone()[0]
    assert count == n_threads * inserts_per_thread

    # Cleanup
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM signals WHERE symbol = 'X'"))

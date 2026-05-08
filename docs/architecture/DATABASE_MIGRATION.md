# Database Migration Runbook — SQLite to Postgres

Status: **draft / opt-in** — production still runs on SQLite. This
document describes how to flip the switch when concurrent-user load
makes that no longer safe (see ADR-001 and SCALING_ROADMAP).

The plumbing (SQLAlchemy 2.x engine, Alembic migrations, driver-aware
helpers) is already in the codebase under `app/db/` and
`backend/alembic.ini`. What's missing is (a) the data copy and (b) the
consumer-file migration from raw `aiosqlite` to the SQLAlchemy session
helpers. Both are mechanical; both are described below.

---

## 0. Why we're doing this

SQLite WAL serializes writes. In our workload that's fine up to ~30
concurrent active users; past that, write latency starts climbing
fast and `database is locked` errors leak into request handlers. The
break-even with the operational cost of running Postgres sits around
50–100 concurrent users. We move *before* we hit it.

## 1. Architecture after the switch

```
                ┌──────────────────────────────┐
                │  app.db.engine.get_engine()  │
                │  reads $DATABASE_URL         │
                └───────┬──────────────────────┘
                        │
        ┌───────────────┴────────────────┐
        │                                │
   sqlite:///./stockpilot.db    postgresql+psycopg://…/agentx
       (local dev)                    (staging / prod)
```

- One env var (`DATABASE_URL`) selects the backend.
- Pool size is `$DB_POOL_SIZE` (default 10), `$DB_MAX_OVERFLOW` (5).
- Schema is owned by Alembic — `app/db/migrations/versions/0001_initial.py`
  captures the current SQLite schema in dialect-portable form.

## 2. Pre-flight (do these in staging first)

1. Provision Postgres 15+ (RDS, Cloud SQL, or self-hosted). One DB per
   environment, one service account per app.
2. Install client deps: `pip install -r backend/requirements.txt`
   (already pulls in `sqlalchemy`, `alembic`, `psycopg[binary]`).
3. Create the database:
   ```sql
   CREATE DATABASE agentx;
   CREATE ROLE agentx_app LOGIN PASSWORD '…';
   GRANT ALL ON DATABASE agentx TO agentx_app;
   ```
4. Smoke-test connectivity:
   ```bash
   DATABASE_URL='postgresql+psycopg://agentx_app:…@host:5432/agentx' \
     python -c "from app.db.engine import get_engine; \
                print(get_engine().connect().execute(__import__('sqlalchemy').text('SELECT 1')).scalar())"
   ```

## 3. Initialize schema on Postgres

Two equivalent ways — pick one and stick with it.

### 3a. Alembic (preferred for prod)

```bash
cd backend
DATABASE_URL='postgresql+psycopg://…/agentx' alembic upgrade head
```

### 3b. App bootstrap (acceptable for staging)

```bash
DATABASE_URL='postgresql+psycopg://…/agentx' \
  python -c "import asyncio, app.database as d; asyncio.run(d.init_db())"
```

Both produce the same schema. Alembic also writes the `alembic_version`
row, which is what production wants.

## 4. Copy data SQLite → Postgres

The simplest reliable path is `pgloader` because it handles type
coercion (TEXT timestamps stay TEXT; INTEGER PKs become BIGINT
identity columns).

```bash
pgloader sqlite:///path/to/stockpilot.db \
         postgresql://agentx_app:…@host:5432/agentx
```

If `pgloader` is unavailable, the CSV pipe works for our row counts
(<10M rows on the largest table):

```bash
sqlite3 stockpilot.db <<'SQL' >/tmp/signals.csv
.headers on
.mode csv
SELECT * FROM signals;
SQL

psql 'postgresql://…/agentx' \
  -c "\copy signals FROM '/tmp/signals.csv' CSV HEADER"
```

Repeat per table. Order matters only if you have FKs (we don't, today).

## 5. Cut over

Sequence (zero-downtime style):

1. **Snapshot** the SQLite file (`cp stockpilot.db stockpilot.db.snap`).
   Keep this until at least 7 days post-cutover.
2. **Drain writes** — put the app in maintenance mode or stop the
   scheduler. Read-only traffic can keep going on SQLite.
3. **Copy** (step 4 above). Verify row counts match per table:
   ```bash
   sqlite3 stockpilot.db 'SELECT COUNT(*) FROM signals;'
   psql … -c 'SELECT COUNT(*) FROM signals;'
   ```
4. **Set `DATABASE_URL`** in the prod env and restart the app:
   ```
   DATABASE_URL=postgresql+psycopg://…/agentx
   DB_POOL_SIZE=10
   ```
5. **Smoke test** — hit `/api/health`, `/api/signals?limit=10`,
   `/api/llm/usage`. Watch error rate for 10 minutes.
6. **Resume** scheduler + writes.

## 6. Consumer-file migration (in-flight)

Today, several consumer files still call `aiosqlite.connect(DB_PATH)`
directly. They keep working only on SQLite. Files that need to be
ported to the SQLAlchemy session helpers before Postgres can serve
production reads:

- `app/routers/signals.py`
- `app/routers/analysis.py`
- `app/routers/alerts.py`
- `app/routers/watchlist.py`
- `app/services/portfolio.py` (other than `ensure_schema`, which
  already uses the driver-aware `database.apply_migration_sql` once it
  switches from `db.executescript`).

Pattern to apply (search-and-replace, mechanical):

```python
# before
async with aiosqlite.connect(DB_PATH) as db:
    db.row_factory = aiosqlite.Row
    cur = await db.execute("SELECT * FROM signals WHERE id = ?", (sig_id,))
    row = await cur.fetchone()

# after
from app.db.engine import get_engine
from sqlalchemy import text

def _fetch_signal(sig_id: str):
    eng = get_engine()
    with eng.connect() as conn:
        return conn.execute(
            text("SELECT * FROM signals WHERE id = :id"), {"id": sig_id}
        ).mappings().first()

row = await asyncio.to_thread(_fetch_signal, sig_id)
```

Track this as a separate ticket per file — small, low-risk,
incrementally landable. The driver-aware helpers in `app.database`
(`record_llm_usage`, `get_today_llm_spend_usd`, `cleanup_old_signals`,
`init_db`, `apply_migration_sql`) are already converted and serve as
reference implementations.

## 7. Rollback

If the Postgres cutover goes wrong:

1. Set `DATABASE_URL` back to the SQLite path (or unset it — the app
   defaults to `settings.sqlite_path`).
2. Restart the app.
3. Restore from the snapshot taken in step 5.1 if data was written
   to Postgres in the bad window:
   ```bash
   cp stockpilot.db.snap stockpilot.db
   ```
4. Investigate before retrying.

The Alembic `downgrade()` for `0001_initial` is intentionally a no-op
that raises — we do *not* destroy data on rollback. Restore from
snapshot is the only sanctioned rollback path.

## 8. Open follow-ups

- `TIMESTAMP WITH TIME ZONE` migration for the `created_at` / `ts`
  columns. Today they're `TEXT` (ISO-8601 UTC strings) for back-compat;
  switching gives us proper time arithmetic in queries.
- Consumer file migration (section 6). Tracked per file.
- Read replica wiring once Postgres is the source of truth.

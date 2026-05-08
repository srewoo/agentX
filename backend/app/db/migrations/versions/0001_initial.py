"""initial schema — captures all tables that existed prior to the SQLAlchemy migration

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-08

Notes
-----
* This migration is idempotent against an already-created SQLite DB. It uses
  `IF NOT EXISTS` for tables and indexes, and stamps the alembic_version table
  on first run.
* Timestamps are stored as TEXT (ISO-8601 UTC strings) — that matches the
  existing on-disk format. Migrating to `TIMESTAMP WITH TIME ZONE` would
  require a data conversion pass and is deliberately deferred to a follow-up
  migration once Postgres is the source of truth.
* `INTEGER PRIMARY KEY AUTOINCREMENT` is SQLite-specific. We emit the right
  flavour per dialect: SQLite gets the legacy form (so existing rows survive),
  Postgres gets `BIGSERIAL`.
"""
from __future__ import annotations

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def _autoincrement_pk(dialect: str) -> str:
    if dialect == "sqlite":
        return "INTEGER PRIMARY KEY AUTOINCREMENT"
    if dialect == "postgresql":
        return "BIGSERIAL PRIMARY KEY"
    # Fallback that most dialects accept.
    return "BIGINT PRIMARY KEY"


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    pk = _autoincrement_pk(dialect)

    op.execute(
        """
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
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_signals_unread ON signals(read, dismissed)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS watchlist (
            symbol TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            exchange TEXT DEFAULT 'NSE',
            added_at TEXT NOT NULL
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )

    op.execute(
        """
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
        )
        """
    )

    op.execute(
        """
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
        )
        """
    )

    op.execute(
        """
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
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_price_alerts_active ON price_alerts(active)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_price_alerts_symbol ON price_alerts(symbol)")

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS backtest_runs (
            id {pk},
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
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_backtest_runs_run_at ON backtest_runs(run_at DESC)")

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS llm_usage (
            id {pk},
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
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts DESC)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_usage_provider_ts ON llm_usage(provider, ts DESC)"
    )

    # Portfolio tables — mirror app/database_migrations/portfolio_tables.sql.
    # The CHECK constraints + partial index work on both SQLite and Postgres.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS holdings (
            id          TEXT PRIMARY KEY,
            symbol      TEXT NOT NULL,
            exchange    TEXT NOT NULL DEFAULT 'NSE',
            qty         REAL NOT NULL,
            avg_price   REAL NOT NULL,
            opened_at   TEXT NOT NULL,
            closed_at   TEXT
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_holdings_symbol ON holdings(symbol)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_holdings_open ON holdings(closed_at) WHERE closed_at IS NULL"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id      TEXT PRIMARY KEY,
            ts      TEXT NOT NULL,
            symbol  TEXT NOT NULL,
            side    TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
            qty     REAL NOT NULL CHECK (qty > 0),
            price   REAL NOT NULL CHECK (price >= 0),
            fees    REAL NOT NULL DEFAULT 0,
            notes   TEXT
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_transactions_ts ON transactions(ts DESC)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_transactions_symbol ON transactions(symbol, ts DESC)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS benchmarks (
            date          TEXT PRIMARY KEY,
            nifty_close   REAL,
            sensex_close  REAL
        )
        """
    )


def downgrade() -> None:
    # Forward-only migration — refuse to drop production data.
    raise NotImplementedError(
        "0001_initial is the baseline; downgrade would destroy data. "
        "Restore from snapshot instead. See "
        "docs/architecture/DATABASE_MIGRATION.md#rollback."
    )

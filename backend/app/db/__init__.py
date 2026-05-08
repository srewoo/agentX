"""Driver-agnostic persistence layer.

Lives next to the legacy `app.database` module (which keeps speaking
SQLite via aiosqlite for the existing async hot paths). This package
provides the SQLAlchemy 2.x engine factory and helpers that let the
project run against either SQLite (local dev, default) or Postgres
(production, opt-in via DATABASE_URL) without changing call sites.

See docs/architecture/DATABASE_MIGRATION.md for the cutover playbook.
"""

from app.db.engine import (
    DEFAULT_SQLITE_URL,
    apply_migration_sql,
    dispose_engine,
    get_dialect,
    get_engine,
    get_session,
    resolve_database_url,
)

__all__ = [
    "DEFAULT_SQLITE_URL",
    "apply_migration_sql",
    "dispose_engine",
    "get_dialect",
    "get_engine",
    "get_session",
    "resolve_database_url",
]

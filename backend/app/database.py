"""SQLite database setup via aiosqlite."""
import logging
import aiosqlite
from app.config import settings

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


async def init_db():
    """Initialize database schema and default settings."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Enable WAL mode for concurrent reads while writing
        await db.execute("PRAGMA journal_mode=WAL;")

        await db.execute(CREATE_SIGNALS_TABLE)
        await db.execute(CREATE_WATCHLIST_TABLE)
        await db.execute(CREATE_SETTINGS_TABLE)
        await db.execute(CREATE_SIGNAL_OUTCOMES_TABLE)
        await db.execute(CREATE_SIGNAL_PERFORMANCE_TABLE)
        await db.execute(CREATE_PRICE_ALERTS_TABLE)

        for idx_sql in CREATE_SIGNALS_INDEXES:
            await db.execute(idx_sql)

        for idx_sql in CREATE_PRICE_ALERTS_INDEXES:
            await db.execute(idx_sql)

        # Seed default settings (INSERT OR IGNORE — don't overwrite user changes)
        for key, value in DEFAULT_SETTINGS.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )

        await db.commit()
    logger.info(f"Database initialized at {DB_PATH}")


async def cleanup_old_signals() -> int:
    """
    Archive signals older than _SIGNAL_MAX_AGE_DAYS.
    Deletes read+dismissed signals; keeps unread ones regardless of age.
    Returns number of rows deleted.
    """
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_SIGNAL_MAX_AGE_DAYS)).isoformat()
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
    if deleted > 0:
        logger.info("Signal cleanup: removed %d old signals (older than %d days)", deleted, _SIGNAL_MAX_AGE_DAYS)
    return deleted


async def get_db() -> aiosqlite.Connection:
    """Return a connected aiosqlite connection. Caller must close."""
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    return conn

from __future__ import annotations

"""Shared pytest fixtures for agentX backend tests."""

import os
import tempfile
from datetime import datetime, timezone

import aiosqlite
import numpy as np
import pandas as pd
import pytest
import pytest_asyncio


# ---------------------------------------------------------------------------
# Ensure the app config points at a temp DB before any app import
# ---------------------------------------------------------------------------

_tmp_db_fd, _tmp_db_path = tempfile.mkstemp(suffix=".db")
os.close(_tmp_db_fd)
os.environ.setdefault("SQLITE_PATH", _tmp_db_path)

# Schema DDL copied from app.database to avoid importing app.config at
# module level (which would read .env and possibly fail in CI).
_SCHEMA_STATEMENTS = [
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
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS watchlist (
        symbol TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        exchange TEXT DEFAULT 'NSE',
        added_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
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
    );
    """,
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
    );
    """,
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
        note TEXT
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_signals_unread ON signals(read, dismissed);",
    "CREATE INDEX IF NOT EXISTS idx_price_alerts_active ON price_alerts(active);",
    "CREATE INDEX IF NOT EXISTS idx_price_alerts_symbol ON price_alerts(symbol);",
]

_DEFAULT_SETTINGS = {
    "alert_interval_minutes": "30",
    "risk_mode": "balanced",
    "signal_types": '["intraday","swing","long_term"]',
    "llm_provider": "gemini",
    "llm_model": "gemini-2.0-flash",
    "llm_api_key": "",
    "openai_api_key": "",
    "gemini_api_key": "",
    "claude_api_key": "",
}


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def tmp_db_path() -> str:
    """Return path to the temporary test database."""
    return _tmp_db_path


@pytest_asyncio.fixture()
async def db(tmp_db_path: str) -> aiosqlite.Connection:
    """Provide a fresh database connection with schema initialized.

    Tables are created once; rows are deleted between tests so each test
    starts with a clean slate.
    """
    async with aiosqlite.connect(tmp_db_path) as conn:
        for stmt in _SCHEMA_STATEMENTS:
            await conn.execute(stmt)

        # Seed default settings
        for key, value in _DEFAULT_SETTINGS.items():
            await conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        await conn.commit()

        yield conn

        # Cleanup: delete all rows so the next test starts clean
        for table in (
            "signals",
            "watchlist",
            "signal_outcomes",
            "signal_performance",
            "price_alerts",
        ):
            await conn.execute(f"DELETE FROM {table}")
        await conn.commit()


# ---------------------------------------------------------------------------
# OHLCV DataFrame fixtures
# ---------------------------------------------------------------------------

def _make_ohlcv(
    rows: int = 100,
    base_price: float = 1500.0,
    volatility: float = 0.02,
    base_volume: float = 1_000_000,
    seed: int = 42,
) -> pd.DataFrame:
    """Generate a synthetic OHLCV DataFrame mimicking Indian stock data.

    Prices are in the INR 100-5000 range with realistic daily moves.
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=datetime.now(), periods=rows)

    closes = [base_price]
    for _ in range(rows - 1):
        ret = rng.normal(0, volatility)
        closes.append(closes[-1] * (1 + ret))
    closes = np.array(closes)

    highs = closes * (1 + rng.uniform(0.002, 0.015, rows))
    lows = closes * (1 - rng.uniform(0.002, 0.015, rows))
    opens = lows + (highs - lows) * rng.uniform(0.2, 0.8, rows)
    volumes = rng.uniform(0.5, 2.0, rows) * base_volume

    df = pd.DataFrame(
        {
            "Open": opens,
            "High": highs,
            "Low": lows,
            "Close": closes,
            "Volume": volumes,
        },
        index=dates,
    )
    return df


@pytest.fixture()
def sample_ohlcv_100() -> pd.DataFrame:
    """100-row OHLCV DataFrame with base price ~INR 1500."""
    return _make_ohlcv(rows=100, base_price=1500.0, seed=42)


@pytest.fixture()
def sample_ohlcv_50() -> pd.DataFrame:
    """50-row OHLCV DataFrame with base price ~INR 2500."""
    return _make_ohlcv(rows=50, base_price=2500.0, seed=99)


@pytest.fixture()
def sample_ohlcv_short() -> pd.DataFrame:
    """Short (10-row) OHLCV DataFrame for edge-case testing."""
    return _make_ohlcv(rows=10, base_price=800.0, seed=7)


# ---------------------------------------------------------------------------
# Signal fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_signal() -> dict:
    """A single realistic signal dict."""
    return {
        "id": "test-signal-001",
        "symbol": "RELIANCE",
        "signal_type": "price_spike",
        "direction": "bullish",
        "strength": 7,
        "reason": "Price moved +5.2% (INR 2400.00 -> INR 2524.80)",
        "risk": "Price spikes can reverse quickly.",
        "llm_summary": None,
        "current_price": 2524.80,
        "metadata": {"change_pct": 5.2, "prev_price": 2400.0},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "read": False,
        "dismissed": False,
    }


@pytest.fixture()
def sample_signals() -> list[dict]:
    """A batch of signals with varying strengths and types."""
    now = datetime.now(timezone.utc).isoformat()
    return [
        {
            "id": f"sig-{i}",
            "symbol": sym,
            "signal_type": stype,
            "direction": direction,
            "strength": strength,
            "reason": f"Test reason {i}",
            "risk": "Test risk",
            "llm_summary": None,
            "current_price": price,
            "metadata": {},
            "created_at": now,
            "read": False,
            "dismissed": False,
        }
        for i, (sym, stype, direction, strength, price) in enumerate(
            [
                ("RELIANCE", "price_spike", "bullish", 8, 2500.0),
                ("TCS", "volume_spike", "neutral", 5, 3800.0),
                ("INFY", "rsi_extreme", "bearish", 6, 1400.0),
                ("HDFCBANK", "macd_crossover", "bullish", 4, 1650.0),
                ("ITC", "breakout", "bullish", 9, 450.0),
                ("SBIN", "sentiment_shift", "bearish", 3, 780.0),
                ("WIPRO", "rsi_extreme", "bullish", 2, 420.0),
            ]
        )
    ]


# ---------------------------------------------------------------------------
# Technicals / S&R fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_technicals() -> dict:
    """Realistic technicals dict as returned by compute_technicals."""
    return {
        "rsi": 55.3,
        "rsi_signal": "Neutral",
        "adx": 28.5,
        "macd": {
            "macd_line": 12.5,
            "macd_line_prev": 11.0,
            "signal_line": 10.0,
            "signal_line_prev": 10.5,
            "histogram": 2.5,
            "signal": "Bullish",
        },
        "moving_averages": {"sma20": 1480.0, "sma50": 1450.0, "sma200": 1400.0, "ema20": 1485.0},
        "bollinger_bands": {"upper": 1560.0, "middle": 1480.0, "lower": 1400.0, "signal": "Normal"},
        "volume_avg_20": 1_200_000,
        "volume_current": 2_800_000,
        "current_price": 1520.0,
        "prev_price": 1500.0,
    }


@pytest.fixture()
def sample_sr() -> dict:
    """Realistic support/resistance dict."""
    return {
        "pivot": 1500.0,
        "resistance": {"r1": 1520.0, "r2": 1550.0, "r3": 1580.0},
        "support": {"s1": 1470.0, "s2": 1440.0, "s3": 1410.0},
    }

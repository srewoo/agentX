from __future__ import annotations

"""Integration tests for the FastAPI API endpoints.

Uses httpx.AsyncClient against the FastAPI app with mocked external
dependencies (database, cache, orchestrator, yfinance, TradingView).
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Ensure test DB path is set before importing the app
_tmp_fd, _api_test_db = tempfile.mkstemp(suffix="_api_test.db")
os.close(_tmp_fd)
os.environ["SQLITE_PATH"] = _api_test_db

# Schema DDL for the API test DB (same as conftest)
_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS signals (
        id TEXT PRIMARY KEY, symbol TEXT NOT NULL, signal_type TEXT NOT NULL,
        direction TEXT NOT NULL, strength INTEGER NOT NULL, reason TEXT NOT NULL,
        risk TEXT, llm_summary TEXT, current_price REAL, metadata TEXT,
        created_at TEXT NOT NULL, read INTEGER DEFAULT 0, dismissed INTEGER DEFAULT 0
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS watchlist (
        symbol TEXT PRIMARY KEY, name TEXT NOT NULL,
        exchange TEXT DEFAULT 'NSE', added_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS signal_outcomes (
        signal_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, signal_type TEXT NOT NULL,
        direction TEXT NOT NULL, entry_price REAL NOT NULL, exit_price REAL,
        entry_time TEXT NOT NULL, exit_time TEXT, pnl_pct REAL, outcome TEXT,
        hold_days INTEGER, evaluated_at TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS signal_performance (
        signal_type TEXT NOT NULL, direction TEXT NOT NULL,
        timeframe TEXT NOT NULL DEFAULT 'all', total_signals INTEGER DEFAULT 0,
        wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0,
        avg_pnl_pct REAL DEFAULT 0, win_rate REAL DEFAULT 0, updated_at TEXT,
        PRIMARY KEY (signal_type, direction, timeframe)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS price_alerts (
        id TEXT PRIMARY KEY, symbol TEXT NOT NULL, target_price REAL NOT NULL,
        condition TEXT NOT NULL, current_price_at_creation REAL,
        created_at TEXT NOT NULL, triggered_at TEXT, triggered_price REAL,
        active INTEGER DEFAULT 1, note TEXT
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_signals_unread ON signals(read, dismissed);",
    "CREATE INDEX IF NOT EXISTS idx_price_alerts_active ON price_alerts(active);",
]


async def _init_test_db():
    """Initialize schema in the API test database."""
    async with aiosqlite.connect(_api_test_db) as db:
        for stmt in _SCHEMA_STATEMENTS:
            await db.execute(stmt)
        # Seed default settings
        for key, value in {
            "alert_interval_minutes": "30",
            "risk_mode": "balanced",
            "llm_provider": "gemini",
            "llm_model": "gemini-2.0-flash",
        }.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        await db.commit()


async def _cleanup_test_db():
    """Remove all rows from test tables."""
    try:
        async with aiosqlite.connect(_api_test_db) as db:
            for table in ("signals", "watchlist", "signal_outcomes", "signal_performance", "price_alerts"):
                await db.execute(f"DELETE FROM {table}")
            await db.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture()
async def client():
    """Provide an httpx AsyncClient wired to the FastAPI app."""

    # Initialize test DB schema BEFORE the app starts
    await _init_test_db()

    with (
        patch("app.main.cache_manager") as mock_cache,
        patch("app.main.orchestrator") as mock_orch,
        patch("app.main.init_db", new_callable=AsyncMock) as mock_init_db,
        patch("app.database.DB_PATH", _api_test_db),
        patch("app.services.alert_checker.DB_PATH", _api_test_db),
        patch("app.services.signal_tracker.DB_PATH", _api_test_db),
        patch("app.routers.signals.DB_PATH", _api_test_db),
        patch("app.routers.market.DB_PATH", _api_test_db),
    ):
        mock_cache.connect = AsyncMock()
        mock_cache.disconnect = AsyncMock()
        mock_cache.enabled = False
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.set = AsyncMock()
        mock_orch.start = AsyncMock()
        mock_orch.stop = AsyncMock()
        mock_orch.is_running = MagicMock(return_value=False)

        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            yield ac

    await _cleanup_test_db()


# ---------------------------------------------------------------------------
# Helper to seed data
# ---------------------------------------------------------------------------

async def _seed_signal(symbol: str = "RELIANCE", strength: int = 7) -> str:
    import uuid
    sig_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(_api_test_db) as db:
        await db.execute(
            """INSERT INTO signals (id, symbol, signal_type, direction, strength,
               reason, risk, current_price, metadata, created_at, read, dismissed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)""",
            (sig_id, symbol, "price_spike", "bullish", strength,
             "Test reason", "Test risk", 2500.0, "{}", now),
        )
        await db.commit()
    return sig_id


async def _seed_alert(symbol: str = "RELIANCE", target: float = 2600.0, condition: str = "above") -> str:
    import uuid
    alert_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(_api_test_db) as db:
        await db.execute(
            """INSERT INTO price_alerts (id, symbol, target_price, condition,
               created_at, active) VALUES (?, ?, ?, ?, ?, 1)""",
            (alert_id, symbol, target, condition, now),
        )
        await db.commit()
    return alert_id


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------

class TestHealthEndpoint:

    @pytest.mark.asyncio
    async def test_given_healthy_server_when_called_then_returns_200_ok(self, client):
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_given_healthy_server_when_called_then_has_db_field(self, client):
        resp = await client.get("/api/health")
        data = resp.json()
        assert "db" in data


# ---------------------------------------------------------------------------
# GET /api/stocks/search
# ---------------------------------------------------------------------------

class TestStockSearch:

    @pytest.mark.asyncio
    async def test_given_query_when_searched_then_returns_results_key(self, client):
        with patch("app.routers.stocks._SEARCH_INDEX", [
            {"symbol": "RELIANCE.NS", "name": "Reliance Industries", "exchange": "NSE", "sector": "Energy"},
            {"symbol": "TCS.NS", "name": "Tata Consultancy Services", "exchange": "NSE", "sector": "IT"},
        ]):
            resp = await client.get("/api/stocks/search?q=reliance")
            assert resp.status_code == 200
            data = resp.json()
            assert "results" in data

    @pytest.mark.asyncio
    async def test_given_empty_query_when_searched_then_returns_default_list(self, client):
        resp = await client.get("/api/stocks/search?q=")
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data


# ---------------------------------------------------------------------------
# GET /api/signals/latest
# ---------------------------------------------------------------------------

class TestSignalsLatest:

    @pytest.mark.asyncio
    async def test_given_empty_db_when_fetched_then_returns_empty_signals(self, client):
        resp = await client.get("/api/signals/latest")
        assert resp.status_code == 200
        data = resp.json()
        assert "signals" in data
        assert isinstance(data["signals"], list)
        assert "unread_count" in data

    @pytest.mark.asyncio
    async def test_given_seeded_signal_when_fetched_then_returns_signal(self, client):
        await _seed_signal()
        resp = await client.get("/api/signals/latest")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["signals"]) >= 1
        assert data["unread_count"] >= 1


# ---------------------------------------------------------------------------
# POST /api/alerts + validation
# ---------------------------------------------------------------------------

class TestAlertsEndpoint:

    @pytest.mark.asyncio
    async def test_given_valid_body_when_posted_then_creates_alert(self, client):
        body = {"symbol": "RELIANCE", "target_price": 2600.0, "condition": "above"}
        resp = await client.post("/api/alerts", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert "alert" in data
        assert data["alert"]["symbol"] == "RELIANCE"
        assert data["alert"]["condition"] == "above"
        assert data["alert"]["active"] is True

    @pytest.mark.asyncio
    async def test_given_below_condition_when_posted_then_creates_alert(self, client):
        body = {"symbol": "TCS", "target_price": 3500.0, "condition": "below"}
        resp = await client.post("/api/alerts", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["alert"]["condition"] == "below"

    @pytest.mark.asyncio
    async def test_given_invalid_condition_when_posted_then_returns_422(self, client):
        body = {"symbol": "TEST", "target_price": 100.0, "condition": "invalid"}
        resp = await client.post("/api/alerts", json=body)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_given_negative_price_when_posted_then_returns_422(self, client):
        body = {"symbol": "TEST", "target_price": -50.0, "condition": "above"}
        resp = await client.post("/api/alerts", json=body)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_given_missing_symbol_when_posted_then_returns_422(self, client):
        body = {"target_price": 100.0, "condition": "above"}
        resp = await client.post("/api/alerts", json=body)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_given_seeded_alerts_when_listed_then_returns_alerts(self, client):
        await _seed_alert("INFY", 1500.0, "above")
        resp = await client.get("/api/alerts")
        assert resp.status_code == 200
        data = resp.json()
        assert "alerts" in data
        assert len(data["alerts"]) >= 1


# ---------------------------------------------------------------------------
# GET /api/screener/presets
# ---------------------------------------------------------------------------

class TestScreenerPresets:

    @pytest.mark.asyncio
    async def test_given_presets_endpoint_when_called_then_returns_presets(self, client):
        with patch("app.routers.screener.SCREENER_PRESETS", {
            "oversold": {"label": "Oversold", "description": "RSI < 30", "params": {"rsi_max": 30}},
            "momentum": {"label": "Momentum", "description": "Strong uptrend", "params": {"rsi_min": 60}},
        }):
            resp = await client.get("/api/screener/presets")
            assert resp.status_code == 200
            data = resp.json()
            assert "presets" in data
            assert "oversold" in data["presets"]
            assert "momentum" in data["presets"]


# ---------------------------------------------------------------------------
# GET /api/performance/summary
# ---------------------------------------------------------------------------

class TestPerformanceSummary:

    @pytest.mark.asyncio
    async def test_given_empty_outcomes_when_fetched_then_returns_zero_stats(self, client):
        resp = await client.get("/api/performance/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data
        summary = data["data"]
        assert summary["total_evaluated"] == 0
        assert summary["win_rate"] == 0.0

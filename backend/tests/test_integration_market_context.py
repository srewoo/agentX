from __future__ import annotations
"""Integration tests for /api/market/context endpoint and signal metadata shape.

Starts the real FastAPI app (with mocked externals) via httpx.AsyncClient,
verifies the new market context endpoint returns the expected shape, and
checks that signals contain new metadata fields.
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

# Ensure test DB path before app import
_tmp_fd, _test_db = tempfile.mkstemp(suffix="_integration_test.db")
os.close(_tmp_fd)
os.environ["SQLITE_PATH"] = _test_db

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS signals (
        id TEXT PRIMARY KEY, symbol TEXT NOT NULL, signal_type TEXT NOT NULL,
        direction TEXT NOT NULL, strength INTEGER NOT NULL, reason TEXT NOT NULL,
        risk TEXT, llm_summary TEXT, current_price REAL, metadata TEXT,
        created_at TEXT NOT NULL, read INTEGER DEFAULT 0, dismissed INTEGER DEFAULT 0
    );""",
    """CREATE TABLE IF NOT EXISTS watchlist (
        symbol TEXT PRIMARY KEY, name TEXT NOT NULL,
        exchange TEXT DEFAULT 'NSE', added_at TEXT NOT NULL
    );""",
    """CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    );""",
    """CREATE TABLE IF NOT EXISTS signal_outcomes (
        signal_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, signal_type TEXT NOT NULL,
        direction TEXT NOT NULL, entry_price REAL NOT NULL, exit_price REAL,
        entry_time TEXT NOT NULL, exit_time TEXT, pnl_pct REAL, outcome TEXT,
        hold_days INTEGER, evaluated_at TEXT
    );""",
    """CREATE TABLE IF NOT EXISTS signal_performance (
        signal_type TEXT NOT NULL, direction TEXT NOT NULL,
        timeframe TEXT NOT NULL DEFAULT 'all', total_signals INTEGER DEFAULT 0,
        wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0,
        avg_pnl_pct REAL DEFAULT 0, win_rate REAL DEFAULT 0, updated_at TEXT,
        PRIMARY KEY (signal_type, direction, timeframe)
    );""",
    """CREATE TABLE IF NOT EXISTS price_alerts (
        id TEXT PRIMARY KEY, symbol TEXT NOT NULL, target_price REAL NOT NULL,
        condition TEXT NOT NULL, current_price_at_creation REAL,
        created_at TEXT NOT NULL, triggered_at TEXT, triggered_price REAL,
        active INTEGER DEFAULT 1, note TEXT
    );""",
]


async def _init_db():
    async with aiosqlite.connect(_test_db) as db:
        for stmt in _SCHEMA:
            await db.execute(stmt)
        for key, val in {"alert_interval_minutes": "30", "risk_mode": "balanced",
                         "llm_provider": "gemini", "llm_model": "gemini-2.0-flash"}.items():
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, val))
        await db.commit()


async def _cleanup():
    try:
        async with aiosqlite.connect(_test_db) as db:
            for t in ("signals", "watchlist", "signal_outcomes", "signal_performance", "price_alerts"):
                await db.execute(f"DELETE FROM {t}")
            await db.commit()
    except Exception:
        pass


@pytest_asyncio.fixture()
async def client():
    await _init_db()

    with (
        patch("app.main.cache_manager") as mock_cache,
        patch("app.main.orchestrator") as mock_orch,
        patch("app.main.init_db", new_callable=AsyncMock),
        patch("app.services.signal_tracker.seed_performance_cache", new_callable=AsyncMock, return_value=0),
        patch("app.database.DB_PATH", _test_db),
        patch("app.services.signal_tracker.DB_PATH", _test_db),
        patch("app.routers.signals.DB_PATH", _test_db),
        patch("app.routers.market.DB_PATH", _test_db),
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

    await _cleanup()


# ─────────────────────────────────────────────
# /api/market/context
# ─────────────────────────────────────────────

class TestMarketContextEndpoint:
    @pytest.mark.asyncio
    async def test_returns_expected_shape(self, client):
        """The endpoint should return fii_dii, india_vix, and market_regime keys."""
        with (
            patch("app.services.fii_dii.get_fii_dii_data", new=AsyncMock(return_value={
                "fii_net": -1800.0, "dii_net": 1200.0, "sentiment": "bearish", "source": "nse",
            })),
            patch("app.services.market_data.get_india_vix", new=AsyncMock(return_value=18.5)),
            patch("app.services.data_fetcher.async_fetch_history", new=AsyncMock(return_value=None)),
        ):
            resp = await client.get("/api/market/context")
        assert resp.status_code == 200
        data = resp.json()
        assert "fii_dii" in data
        assert "india_vix" in data
        assert "market_regime" in data

    @pytest.mark.asyncio
    async def test_fii_dii_fields(self, client):
        with (
            patch("app.services.fii_dii.get_fii_dii_data", new=AsyncMock(return_value={
                "fii_net": 2500.0, "dii_net": -800.0, "sentiment": "bullish", "source": "nse",
            })),
            patch("app.services.market_data.get_india_vix", new=AsyncMock(return_value=None)),
            patch("app.services.data_fetcher.async_fetch_history", new=AsyncMock(return_value=None)),
        ):
            resp = await client.get("/api/market/context")
        fii = resp.json()["fii_dii"]
        assert fii["fii_net"] == 2500.0
        assert fii["dii_net"] == -800.0
        assert fii["sentiment"] == "bullish"

    @pytest.mark.asyncio
    async def test_graceful_failure_returns_nulls(self, client):
        """If all data sources fail, should still return 200 with null values."""
        with (
            patch("app.services.fii_dii.get_fii_dii_data", new=AsyncMock(side_effect=Exception("fail"))),
            patch("app.services.market_data.get_india_vix", new=AsyncMock(side_effect=Exception("fail"))),
            patch("app.services.data_fetcher.async_fetch_history", new=AsyncMock(side_effect=Exception("fail"))),
        ):
            resp = await client.get("/api/market/context")
        assert resp.status_code == 200
        data = resp.json()
        assert data["fii_dii"] is None
        assert data["india_vix"] is None
        assert data["market_regime"] is None


# ─────────────────────────────────────────────
# Signal metadata shape verification
# ─────────────────────────────────────────────

class TestSignalMetadataShape:
    @pytest.mark.asyncio
    async def test_signal_with_new_metadata_fields_stored_and_retrieved(self, client):
        """Signals with fii_modifier, rs_rank, delivery_pct in metadata should
        round-trip through the DB and API correctly."""
        sig_id = "test-meta-001"
        now = datetime.now(timezone.utc).isoformat()
        metadata = json.dumps({
            "fii_modifier": -2,
            "fii_net": -2000.0,
            "rs_rank": 85,
            "rs_modifier": 1,
            "delivery_pct": 72.5,
            "volume_ratio": 3.2,
            "contributing_signals": ["rsi_extreme", "volume_spike"],
            "signal_count": 2,
        })

        async with aiosqlite.connect(_test_db) as db:
            await db.execute(
                """INSERT INTO signals (id, symbol, signal_type, direction, strength,
                   reason, risk, current_price, metadata, created_at, read, dismissed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)""",
                (sig_id, "RELIANCE", "confluence", "bullish", 9,
                 "Multi-signal bullish confluence", "Manage risk normally.", 2500.0,
                 metadata, now),
            )
            await db.commit()

        resp = await client.get("/api/signals/latest?limit=5")
        assert resp.status_code == 200
        signals = resp.json()["signals"]
        assert len(signals) >= 1

        found = next((s for s in signals if s["id"] == sig_id), None)
        assert found is not None
        assert found["signal_type"] == "confluence"
        assert found["strength"] == 9

        # Metadata should be parsed back to a dict
        meta = found.get("metadata")
        if isinstance(meta, str):
            meta = json.loads(meta)
        assert meta["fii_modifier"] == -2
        assert meta["rs_rank"] == 85
        assert meta["delivery_pct"] == 72.5
        assert "contributing_signals" in meta

    @pytest.mark.asyncio
    async def test_health_endpoint_includes_orchestrator_status(self, client):
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "market_open" in data
        assert "orchestrator_running" in data

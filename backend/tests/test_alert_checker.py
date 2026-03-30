from __future__ import annotations
"""Tests for app.services.alert_checker — price alert CRUD and trigger evaluation."""

import pytest
import pytest_asyncio

from app.services.alert_checker import (
    create_alert,
    get_active_alerts,
    delete_alert,
    check_alerts,
)
from app.database import DB_PATH


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

async def _seed_alert(db, symbol="RELIANCE", target=2500.0, condition="above",
                      creation_price=2400.0, note=None):
    """Insert an alert directly via the db fixture to avoid module-level DB_PATH issues."""
    import uuid
    from datetime import datetime, timezone
    alert_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO price_alerts
           (id, symbol, target_price, condition, current_price_at_creation,
            created_at, active, note)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
        (alert_id, symbol.upper(), target, condition, creation_price, now, note),
    )
    await db.commit()
    return alert_id


# ─────────────────────────────────────────────
# create_alert
# ─────────────────────────────────────────────

class TestCreateAlert:
    @pytest.mark.asyncio
    async def test_creates_alert_returns_dict(self, tmp_db_path):
        import unittest.mock as mock
        with mock.patch("app.services.alert_checker.DB_PATH", tmp_db_path):
            result = await create_alert("RELIANCE", 2600.0, "above", 2400.0, "test note")

        assert result["symbol"] == "RELIANCE"
        assert result["target_price"] == 2600.0
        assert result["condition"] == "above"
        assert result["active"] is True
        assert result["note"] == "test note"
        assert result["id"] is not None

    @pytest.mark.asyncio
    async def test_symbol_uppercased(self, tmp_db_path):
        import unittest.mock as mock
        with mock.patch("app.services.alert_checker.DB_PATH", tmp_db_path):
            result = await create_alert("reliance", 2000.0, "below")
        assert result["symbol"] == "RELIANCE"

    @pytest.mark.asyncio
    async def test_no_note_stored_as_none(self, tmp_db_path):
        import unittest.mock as mock
        with mock.patch("app.services.alert_checker.DB_PATH", tmp_db_path):
            result = await create_alert("TCS", 4000.0, "above")
        assert result["note"] is None


# ─────────────────────────────────────────────
# get_active_alerts
# ─────────────────────────────────────────────

class TestGetActiveAlerts:
    @pytest.mark.asyncio
    async def test_returns_only_active_alerts(self, db, tmp_db_path):
        # Seed one active, one triggered
        active_id = await _seed_alert(db, "RELIANCE", target=2500.0, condition="above")
        # Manually mark second as inactive
        triggered_id = await _seed_alert(db, "TCS", target=4000.0, condition="above")
        await db.execute("UPDATE price_alerts SET active = 0 WHERE id = ?", (triggered_id,))
        await db.commit()

        import unittest.mock as mock
        with mock.patch("app.services.alert_checker.DB_PATH", tmp_db_path):
            alerts = await get_active_alerts()

        ids = [a["id"] for a in alerts]
        assert active_id in ids
        assert triggered_id not in ids

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_alerts(self, tmp_db_path):
        import unittest.mock as mock
        with mock.patch("app.services.alert_checker.DB_PATH", tmp_db_path):
            alerts = await get_active_alerts()
        assert alerts == []


# ─────────────────────────────────────────────
# delete_alert
# ─────────────────────────────────────────────

class TestDeleteAlert:
    @pytest.mark.asyncio
    async def test_delete_existing_returns_true(self, db, tmp_db_path):
        alert_id = await _seed_alert(db)
        import unittest.mock as mock
        with mock.patch("app.services.alert_checker.DB_PATH", tmp_db_path):
            result = await delete_alert(alert_id)
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self, tmp_db_path):
        import unittest.mock as mock
        with mock.patch("app.services.alert_checker.DB_PATH", tmp_db_path):
            result = await delete_alert("nonexistent-id")
        assert result is False


# ─────────────────────────────────────────────
# check_alerts
# ─────────────────────────────────────────────

class TestCheckAlerts:
    @pytest.mark.asyncio
    async def test_empty_prices_returns_empty(self, tmp_db_path):
        import unittest.mock as mock
        with mock.patch("app.services.alert_checker.DB_PATH", tmp_db_path):
            result = await check_alerts({})
        assert result == []

    @pytest.mark.asyncio
    async def test_above_condition_triggered_when_price_meets_target(self, db, tmp_db_path):
        await _seed_alert(db, "RELIANCE", target=2500.0, condition="above", creation_price=2400.0)
        import unittest.mock as mock
        with mock.patch("app.services.alert_checker.DB_PATH", tmp_db_path):
            signals = await check_alerts({"RELIANCE": 2550.0})

        assert len(signals) == 1
        assert signals[0]["signal_type"] == "price_alert"
        assert signals[0]["direction"] == "bullish"
        assert signals[0]["symbol"] == "RELIANCE"

    @pytest.mark.asyncio
    async def test_above_condition_not_triggered_when_below_target(self, db, tmp_db_path):
        await _seed_alert(db, "RELIANCE", target=2500.0, condition="above", creation_price=2400.0)
        import unittest.mock as mock
        with mock.patch("app.services.alert_checker.DB_PATH", tmp_db_path):
            signals = await check_alerts({"RELIANCE": 2400.0})
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_below_condition_triggered(self, db, tmp_db_path):
        await _seed_alert(db, "TCS", target=3500.0, condition="below", creation_price=3800.0)
        import unittest.mock as mock
        with mock.patch("app.services.alert_checker.DB_PATH", tmp_db_path):
            signals = await check_alerts({"TCS": 3400.0})

        assert len(signals) == 1
        assert signals[0]["direction"] == "bearish"

    @pytest.mark.asyncio
    async def test_triggered_alert_marked_inactive(self, db, tmp_db_path):
        alert_id = await _seed_alert(db, "INFY", target=1500.0, condition="above", creation_price=1400.0)
        import unittest.mock as mock
        with mock.patch("app.services.alert_checker.DB_PATH", tmp_db_path):
            await check_alerts({"INFY": 1600.0})
            # Check DB state
            import aiosqlite
            async with aiosqlite.connect(tmp_db_path) as conn:
                cursor = await conn.execute(
                    "SELECT active FROM price_alerts WHERE id = ?", (alert_id,)
                )
                row = await cursor.fetchone()
        assert row[0] == 0  # inactive

    @pytest.mark.asyncio
    async def test_pct_change_condition_triggered(self, db, tmp_db_path):
        """pct_change: triggers when abs(change) >= target_price (used as threshold)."""
        await _seed_alert(db, "WIPRO", target=5.0, condition="pct_change", creation_price=400.0)
        import unittest.mock as mock
        with mock.patch("app.services.alert_checker.DB_PATH", tmp_db_path):
            signals = await check_alerts({"WIPRO": 425.0})  # +6.25% > 5% threshold

        assert len(signals) == 1

    @pytest.mark.asyncio
    async def test_no_signal_for_symbol_not_in_prices(self, db, tmp_db_path):
        await _seed_alert(db, "SBIN", target=800.0, condition="above", creation_price=700.0)
        import unittest.mock as mock
        with mock.patch("app.services.alert_checker.DB_PATH", tmp_db_path):
            signals = await check_alerts({"RELIANCE": 2500.0})  # SBIN not in prices
        assert len(signals) == 0

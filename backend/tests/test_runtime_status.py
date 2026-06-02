from __future__ import annotations
"""Tests for runtime_status heartbeats + the daily-backtest schedule helper."""
import os
import tempfile
from datetime import timedelta

import pytest

from app.services import runtime_status
from app.services.orchestrator import _next_daily_at


@pytest.fixture
def status_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(runtime_status, "DB_PATH", path)
    yield path
    os.unlink(path)


@pytest.mark.asyncio
async def test_record_and_get_roundtrip(status_db):
    await runtime_status.record_run("auto_paper", summary={"opened": 2, "closed": 1})
    status = await runtime_status.get_status()
    assert "auto_paper" in status
    assert status["auto_paper"]["summary"] == {"opened": 2, "closed": 1}
    assert status["auto_paper"]["last_run_at"]  # ISO timestamp present


@pytest.mark.asyncio
async def test_record_upserts(status_db):
    await runtime_status.record_run("scan")
    first = (await runtime_status.get_status())["scan"]["last_run_at"]
    await runtime_status.record_run("scan", summary={"n": 5})
    second = await runtime_status.get_status()
    # One row (upsert, not insert), summary updated.
    assert len(second) == 1
    assert second["scan"]["summary"] == {"n": 5}
    assert second["scan"]["last_run_at"] >= first


@pytest.mark.asyncio
async def test_get_status_empty(status_db):
    assert await runtime_status.get_status() == {}


@pytest.mark.asyncio
async def test_record_run_never_raises_on_bad_db(monkeypatch):
    monkeypatch.setattr(runtime_status, "DB_PATH", "/nonexistent/dir/x.db")
    # Must not raise — heartbeats are best-effort.
    await runtime_status.record_run("scan")


class TestNextDailyAt:
    def test_resolves_to_target_ist_time(self):
        # 11:00 IST == 05:30 UTC.
        dt = _next_daily_at(11, 0)
        ist = dt + timedelta(hours=5, minutes=30)
        assert (ist.hour, ist.minute) == (11, 0)

    def test_is_in_the_future(self):
        from datetime import datetime, timezone
        assert _next_daily_at(11, 0) > datetime.now(timezone.utc)

"""Async-scan lifecycle tests.

The manual scan trigger must:
  1. Return a job_id immediately (no awaiting the full scan).
  2. Expose progress via get_scan_status() while running.
  3. Be idempotent — re-triggering while running returns the same job_id.
  4. Mark itself completed (or failed) once the underlying run finishes.

We stub out `run_scan_cycle` so the lifecycle can be exercised in <1s.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.services import orchestrator as orch_mod
from app.services.orchestrator import (
    _scan_state,
    get_scan_status,
    start_manual_scan,
)


@pytest.fixture(autouse=True)
def _reset_state():
    """Clean slate per-test — module-level _scan_state is shared."""
    _scan_state.job_id = None
    _scan_state.status = "idle"
    _scan_state.started_at = None
    _scan_state.completed_at = None
    _scan_state.duration_ms = None
    _scan_state.total_symbols = 0
    _scan_state.completed_symbols = 0
    _scan_state.current_symbol = None
    _scan_state.signals_so_far = 0
    _scan_state.error = None
    yield


@pytest.mark.asyncio
async def test_start_manual_scan_returns_job_id_immediately():
    """The 202 path must not block on the scan body."""
    async def slow_scan():
        await asyncio.sleep(0.5)
        return []

    with patch.object(orch_mod, "run_scan_cycle", slow_scan):
        result = await start_manual_scan()
        assert result["job_id"]
        assert result["status"] == "running"
        assert result["already_running"] is False
        # The scan task is still in flight — give it time to finish so it
        # doesn't leak into the next test.
        await asyncio.sleep(0.7)


@pytest.mark.asyncio
async def test_retrigger_while_running_returns_same_job_id():
    """Second POST during an in-flight scan is idempotent."""
    async def slow_scan():
        await asyncio.sleep(0.5)
        return []

    with patch.object(orch_mod, "run_scan_cycle", slow_scan):
        first = await start_manual_scan()
        second = await start_manual_scan()
        assert second["job_id"] == first["job_id"]
        assert second["already_running"] is True
        await asyncio.sleep(0.7)


@pytest.mark.asyncio
async def test_scan_state_marks_completed_after_success():
    async def quick_scan():
        return []

    with patch.object(orch_mod, "run_scan_cycle", quick_scan):
        await start_manual_scan()
        # Let the background task drain.
        for _ in range(20):
            await asyncio.sleep(0.05)
            status = await get_scan_status()
            if status["status"] in ("completed", "failed"):
                break

    final = await get_scan_status()
    assert final["status"] == "completed"
    assert final["completed_at"] is not None
    assert final["duration_ms"] is not None
    assert final["error"] is None


@pytest.mark.asyncio
async def test_scan_state_marks_failed_on_exception():
    async def broken_scan():
        raise RuntimeError("yfinance exploded")

    with patch.object(orch_mod, "run_scan_cycle", broken_scan):
        await start_manual_scan()
        for _ in range(20):
            await asyncio.sleep(0.05)
            status = await get_scan_status()
            if status["status"] in ("completed", "failed"):
                break

    final = await get_scan_status()
    assert final["status"] == "failed"
    assert "yfinance exploded" in (final["error"] or "")


@pytest.mark.asyncio
async def test_status_progress_percent_handles_zero_total():
    """progress_pct must not divide-by-zero when no scan has started."""
    status = await get_scan_status()
    assert status["progress_pct"] == 0.0
    assert status["status"] == "idle"

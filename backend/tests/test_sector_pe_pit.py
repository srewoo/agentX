from __future__ import annotations
"""3.5 — point-in-time sector-PE snapshots."""
import os
import tempfile
from datetime import date

import pytest

from app.services import sector_pe_pit as sp


# ── pure as-of picker ──
def test_pick_asof_returns_most_recent_on_or_before():
    snaps = [("2024-01-01", 20.0), ("2024-06-01", 25.0), ("2025-01-01", 30.0)]
    assert sp.pick_asof(snaps, date(2024, 6, 15)) == 25.0   # latest ≤ asof
    assert sp.pick_asof(snaps, date(2024, 6, 1)) == 25.0    # boundary inclusive
    assert sp.pick_asof(snaps, date(2025, 6, 1)) == 30.0


def test_pick_asof_none_when_all_newer():
    snaps = [("2025-01-01", 30.0)]
    assert sp.pick_asof(snaps, date(2024, 6, 1)) is None     # no PIT value that old


# ── store ──
@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    os.unlink(path)


@pytest.mark.asyncio
async def test_save_and_resolve_pit(db):
    await sp.save_snapshot({"IT": 28.0, "Banking": 15.0}, asof=date(2024, 1, 1), db_path=db)
    await sp.save_snapshot({"IT": 32.0, "Banking": 16.0}, asof=date(2025, 1, 1), db_path=db)
    # As of mid-2024 → the 2024 snapshot, not the newer 2025 one.
    assert await sp.get_sector_pe_at("IT", date(2024, 6, 1), db_path=db) == 28.0
    # As of 2025 → the latest.
    assert await sp.get_sector_pe_at("it", date(2025, 6, 1), db_path=db) == 32.0
    # Before any snapshot → None (caller falls back to current lookup).
    assert await sp.get_sector_pe_at("IT", date(2023, 1, 1), db_path=db) is None


@pytest.mark.asyncio
async def test_has_snapshot_before(db):
    assert await sp.has_snapshot_before(date(2024, 6, 1), db_path=db) is False
    await sp.save_snapshot({"IT": 28.0}, asof=date(2024, 1, 1), db_path=db)
    assert await sp.has_snapshot_before(date(2024, 6, 1), db_path=db) is True
    assert await sp.has_snapshot_before(date(2023, 1, 1), db_path=db) is False


@pytest.mark.asyncio
async def test_save_snapshot_ignores_nonpositive_pe(db):
    n = await sp.save_snapshot({"IT": 28.0, "Junk": 0.0, "Bad": -5.0}, asof=date(2024, 1, 1), db_path=db)
    assert await sp.get_sector_pe_at("Junk", date(2024, 6, 1), db_path=db) is None
    assert await sp.get_sector_pe_at("IT", date(2024, 6, 1), db_path=db) == 28.0

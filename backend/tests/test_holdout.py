from __future__ import annotations
"""1.2 — Final untouched holdout: boundary math, immutable pin, trim enforcement."""
import os
import sqlite3
import tempfile
from datetime import date

import pandas as pd
import pytest

from app.database import CREATE_SETTINGS_TABLE
from app.services import holdout


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute(CREATE_SETTINGS_TABLE)
    con.commit()
    con.close()
    holdout._clear_cache()
    yield path
    os.unlink(path)
    holdout._clear_cache()


def _daily(start: str, n: int) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="D")
    return pd.DataFrame({"Close": range(n)}, index=idx)


# ── boundary math ──
def test_boundary_is_12_months_before_reservation():
    b = holdout.boundary_for_reservation(date(2026, 7, 2))
    assert b == date(2025, 7, 2)


def test_boundary_clamps_day_end_of_month():
    # Reserving on the 31st → target month may lack a 31st; clamp is safe.
    b = holdout.boundary_for_reservation(date(2026, 3, 31))
    assert b == date(2025, 3, 28)


# ── trim enforcement ──
def test_trim_drops_bars_after_boundary():
    df = _daily("2026-01-01", 100)          # through 2026-04-10
    trimmed = holdout.trim_history(df, date(2026, 2, 1))
    assert trimmed.index.max() <= pd.Timestamp("2026-02-01")
    assert len(trimmed) == 32               # Jan 1..Feb 1 inclusive


def test_trim_noop_when_unpinned_or_referee():
    df = _daily("2026-01-01", 50)
    assert len(holdout.trim_history(df, None)) == 50          # unpinned → no-op
    assert len(holdout.trim_history(df, date(2026, 1, 5), referee=True)) == 50


def test_trim_fail_open_on_bad_index():
    df = pd.DataFrame({"Close": [1, 2, 3]})   # RangeIndex, not datetimes
    # Must not raise; returns something usable rather than crashing a backtest.
    out = holdout.trim_history(df, date(2026, 1, 1))
    assert out is not None


# ── immutable pin ──
@pytest.mark.asyncio
async def test_pin_is_immutable(db_path):
    first = await holdout.pin_boundary(today=date(2026, 7, 2), db_path=db_path)
    assert first["created"] is True
    assert first["boundary"] == "2025-07-02"
    # A later pin (even a different date) must NOT move the reserved window.
    second = await holdout.pin_boundary(today=date(2026, 12, 31), db_path=db_path)
    assert second["created"] is False
    assert second["boundary"] == "2025-07-02"


@pytest.mark.asyncio
async def test_resolve_reads_pinned_boundary(db_path):
    assert await holdout.resolve_boundary(db_path=db_path) is None
    await holdout.pin_boundary(today=date(2026, 7, 2), db_path=db_path)
    assert await holdout.resolve_boundary(db_path=db_path) == date(2025, 7, 2)


@pytest.mark.asyncio
async def test_env_override_wins(db_path, monkeypatch):
    await holdout.pin_boundary(today=date(2026, 7, 2), db_path=db_path)
    monkeypatch.setenv("AGENTX_HOLDOUT_BOUNDARY", "2024-01-15")
    assert await holdout.resolve_boundary(db_path=db_path) == date(2024, 1, 15)

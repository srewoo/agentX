from __future__ import annotations
"""2.1 — pipeline bake-off: survivor decision + no-deletion-without-forward gate."""
import os
import sqlite3
import tempfile

import pytest

from app.database import CREATE_PAPER_TRADES_TABLE, CREATE_SETTINGS_TABLE
from app.services import pipeline_bakeoff as bo


def _sample(mean: float, n: int) -> list[float]:
    # n trades centered near `mean` with a spread so win rate is realistic.
    return [mean + (1.0 if i % 2 == 0 else -1.0) for i in range(n)]


def test_inconclusive_when_samples_thin():
    v = bo.compare(_sample(1.0, 10), _sample(0.5, 10))
    assert v.survivor is None
    assert "inconclusive" in v.reason


def test_clear_winner_on_expectancy_and_wilson():
    # A: strongly positive; B: negative. A must win on both expectancy and LB.
    a = [3.0] * 40 + [-1.0] * 20     # 40 wins / 60 → high WR, +ve expectancy
    b = [-2.0] * 40 + [1.0] * 20     # 20 wins / 60 → low WR, -ve expectancy
    v = bo.compare(a, b)
    assert v.survivor == "A"
    assert v.a.expectancy_pct > v.b.expectancy_pct


def test_expectancy_leader_trailing_on_winrate_is_inconclusive():
    # A: few huge wins, many losses (high expectancy, low win rate).
    a = [50.0] * 10 + [-1.0] * 50     # 10/60 wins but big expectancy
    # B: many small wins (lower expectancy, much higher win rate / Wilson-LB).
    b = [0.5] * 55 + [-0.4] * 5       # 55/60 wins
    v = bo.compare(a, b)
    # A leads expectancy but trails Wilson-LB WR → refuse to crown it.
    assert v.survivor is None
    assert "Wilson-LB" in v.reason


def test_caveat_always_present():
    v = bo.compare(_sample(1.0, 60), _sample(0.5, 60))
    assert "ADVISORY" in v.caveat


# ── deletion gate ──
@pytest.fixture
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute(CREATE_PAPER_TRADES_TABLE)
    con.execute(CREATE_SETTINGS_TABLE)
    con.commit()
    con.close()
    yield path
    os.unlink(path)


@pytest.mark.asyncio
async def test_deletion_blocked_until_forward_evidence(db):
    # No forward trades → deletion must be BLOCKED regardless of any backtest.
    res = await bo.deletion_authorized(db_path=db)
    assert res["authorized"] is False
    assert "BLOCKED" in res["reason"]
    assert res["required"] == 300

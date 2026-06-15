from __future__ import annotations
"""A4 — champion/challenger promotion + auto-rollback."""
import os
import sqlite3
import tempfile

import pytest

from app.services import champion_challenger as cc


def test_promote_when_challenger_beats_by_margin_on_sample():
    d = cc.challenger_promotion_decision(
        {"expectancy_pct": 0.10, "trades": 200},
        {"expectancy_pct": 0.30, "trades": 200})
    assert d["promote"] is True


def test_no_promote_when_challenger_sample_too_small():
    d = cc.challenger_promotion_decision(
        {"expectancy_pct": 0.10, "trades": 200},
        {"expectancy_pct": 0.90, "trades": 10})
    assert d["promote"] is False


def test_no_promote_without_margin():
    d = cc.challenger_promotion_decision(
        {"expectancy_pct": 0.20, "trades": 200},
        {"expectancy_pct": 0.25, "trades": 200}, margin_pct=0.10)
    assert d["promote"] is False


def test_rollback_when_below_floor_on_sample():
    assert cc.rollback_decision({"expectancy_pct": -0.50, "trades": 100})["rollback"] is True


def test_no_rollback_small_sample():
    assert cc.rollback_decision({"expectancy_pct": -0.90, "trades": 5})["rollback"] is False


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.commit(); con.close()
    yield path
    os.unlink(path)


@pytest.mark.asyncio
async def test_evaluate_promotes_challenger_to_champion(db_path):
    await cc.set_challenger({"promoted": ["double_top|bearish"]}, db_path=db_path)
    res = await cc.evaluate_and_maybe_promote(
        {"expectancy_pct": 0.05, "trades": 200},
        {"expectancy_pct": 0.40, "trades": 200},
        db_path=db_path)
    assert res["action"] == "promoted"
    champ = await cc._get(cc._CHAMPION_KEY, db_path)
    assert champ == {"promoted": ["double_top|bearish"]}


@pytest.mark.asyncio
async def test_evaluate_rolls_back_failing_champion(db_path):
    res = await cc.evaluate_and_maybe_promote(
        {"expectancy_pct": -0.60, "trades": 150},
        {"expectancy_pct": 0.0, "trades": 5},   # challenger too small to promote
        db_path=db_path)
    assert res["action"] == "rolled_back"


@pytest.mark.asyncio
async def test_evaluate_holds_when_neither(db_path):
    res = await cc.evaluate_and_maybe_promote(
        {"expectancy_pct": 0.10, "trades": 200},
        {"expectancy_pct": 0.12, "trades": 200},
        db_path=db_path)
    assert res["action"] == "held"

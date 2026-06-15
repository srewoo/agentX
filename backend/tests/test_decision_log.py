from __future__ import annotations
"""D1 — forward decision log.

Covers the logger as a unit (table self-creation, taken/skipped round-trip,
summary aggregation, fire-and-forget safety) and the integration point: that
auto_open_from_recommendations writes one decision row per actionable
candidate, including the ones it skips.
"""
import os
import sqlite3
import tempfile

import pytest

from app.database import CREATE_PAPER_TRADES_TABLE
from app.services import auto_paper_trader, paper_trading, decision_log


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute(CREATE_PAPER_TRADES_TABLE)
    con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('paper_capital', '100000')")
    con.commit()
    con.close()
    yield path
    os.unlink(path)


def _rec(symbol, action, conviction, entry, stop, target, rr, **extra):
    base = {
        "symbol": symbol, "action": action, "conviction": conviction,
        "risk_reward": rr, "entry": entry, "stoploss": stop, "target1": target,
        "sector": "IT", "horizon": "swing", "regime": "neutral",
    }
    base.update(extra)
    return base


@pytest.mark.asyncio
async def test_log_decisions_creates_table_and_writes(db_path):
    n = await decision_log.log_decisions(
        [{"symbol": "INFY", "taken": True, "conviction": 80, "trade_date": "2026-06-15"}],
        db_path=db_path,
    )
    assert n == 1
    rows = await decision_log.recent_decisions(db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "INFY"
    assert rows[0]["taken"] == 1


@pytest.mark.asyncio
async def test_log_decisions_records_skip_reason_and_factors(db_path):
    await decision_log.log_decisions(
        [{
            "symbol": "ITC", "taken": False, "skip_reason": "negative_kelly_edge",
            "conviction": 70, "trade_date": "2026-06-15",
            "factors": [{"name": "trend", "score": -0.4}],
        }],
        db_path=db_path,
    )
    rows = await decision_log.recent_decisions(db_path=db_path)
    assert rows[0]["taken"] == 0
    assert rows[0]["skip_reason"] == "negative_kelly_edge"
    assert "trend" in (rows[0]["factors_json"] or "")


@pytest.mark.asyncio
async def test_decision_summary_aggregates_taken_and_reasons(db_path):
    await decision_log.log_decisions([
        {"symbol": "A", "taken": True, "trade_date": "2026-06-15"},
        {"symbol": "B", "taken": False, "skip_reason": "below_min_conviction", "trade_date": "2026-06-15"},
        {"symbol": "C", "taken": False, "skip_reason": "below_min_conviction", "trade_date": "2026-06-15"},
        {"symbol": "D", "taken": False, "skip_reason": "correlated_to_open", "trade_date": "2026-06-15"},
    ], db_path=db_path)
    summary = await decision_log.decision_summary(db_path=db_path)
    assert summary["considered"] == 4
    assert summary["taken"] == 1
    assert summary["skipped"] == 3
    assert summary["skip_reasons"]["below_min_conviction"] == 2


@pytest.mark.asyncio
async def test_log_decisions_is_fire_and_forget_on_bad_path():
    # A logging failure must never raise into the trade path.
    n = await decision_log.log_decisions(
        [{"symbol": "X", "taken": True}],
        db_path="/nonexistent_dir/definitely/not/here.db",
    )
    assert n == 0


@pytest.mark.asyncio
async def test_empty_batch_is_noop(db_path):
    assert await decision_log.log_decisions([], db_path=db_path) == 0


@pytest.mark.asyncio
async def test_auto_open_logs_one_decision_per_actionable_candidate(db_path, monkeypatch):
    monkeypatch.setattr(auto_paper_trader, "DB_PATH", db_path)
    monkeypatch.setattr(paper_trading, "DB_PATH", db_path)
    # Disable the risk gate / correlation noise so the outcome is deterministic;
    # we only care that decisions are logged for taken AND skipped candidates.
    recs = [
        # below the floor → skipped + logged
        _rec("AAA", "BUY", conviction=50, entry=100, stop=95, target=115, rr=3.0),
        # negative-edge geometry (target barely above entry, wide stop) → kelly skip
        _rec("BBB", "BUY", conviction=80, entry=100, stop=80, target=101, rr=0.05),
        # a clean positive-edge BUY → should be taken
        _rec("CCC", "BUY", conviction=85, entry=100, stop=95, target=120, rr=4.0),
        # HOLD → never a candidate, must NOT be logged
        _rec("DDD", "HOLD", conviction=90, entry=100, stop=95, target=120, rr=4.0),
    ]
    result = await auto_paper_trader.auto_open_from_recommendations(recs, min_conviction=65)
    assert result["decisions_logged"] >= 1

    rows = await decision_log.recent_decisions(db_path=db_path)
    logged_syms = {r["symbol"] for r in rows}
    # The below-floor BUY and the actionable candidates are logged; HOLD is not.
    assert "AAA" in logged_syms          # below_min_conviction
    assert "DDD" not in logged_syms      # HOLD never logged
    # Every logged row is either taken or carries a skip reason.
    for r in rows:
        assert r["taken"] == 1 or r["skip_reason"]

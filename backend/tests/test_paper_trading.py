from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from app.database import CREATE_PAPER_TRADES_TABLE
from app.services import paper_trading


@pytest.fixture
def paper_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute(CREATE_PAPER_TRADES_TABLE)
    con.commit()
    con.close()
    monkeypatch.setattr(paper_trading, "DB_PATH", path)
    yield path
    os.unlink(path)


@pytest.mark.asyncio
async def test_import_paper_trades_csv_upserts_trailing_stop(paper_db, tmp_path):
    csv_path = tmp_path / "trades.csv"
    csv_path.write_text(
        "trade_id,symbol,direction,signal_type,strength,entry_price,entry_date,"
        "stop_loss,target,position_size,shares,status,exit_price,exit_date,"
        "pnl_pct,pnl_amount,exit_reason,trailing_stop\n"
        "abc,RELIANCE,bullish,gap_up,8,100,2026-05-01,95,110,10000,100,"
        "open,,,,,,96\n",
        encoding="utf-8",
    )

    result = await paper_trading.import_paper_trades_csv(csv_path)
    assert result == {"imported": 1, "skipped": 0}

    con = sqlite3.connect(paper_db)
    row = con.execute(
        "SELECT symbol, signal_type, trailing_stop FROM paper_trades WHERE trade_id='abc'"
    ).fetchone()
    con.close()
    assert row == ("RELIANCE", "gap_up", 96.0)


def test_paper_trade_gate_rejects_weak_unconfirmed_signal():
    gate = paper_trading.paper_trade_gate(
        {
            "signal_type": "double_bottom",
            "direction": "bullish",
            "metadata": {"downgraded_by_edge_filter": True},
        }
    )
    assert gate["allowed"] is False
    assert gate["reason"] == "weak_unconfirmed_edge"


def test_paper_trade_gate_allows_positive_edge_signal():
    gate = paper_trading.paper_trade_gate(
        {"signal_type": "gap_up", "direction": "bullish", "metadata": {}}
    )
    assert gate == {"allowed": True, "reason": "passed_edge_gate"}


@pytest.mark.asyncio
async def test_create_close_list_and_summary_paper_trade(paper_db):
    trade = await paper_trading.create_paper_trade(
        symbol="RELIANCE",
        direction="bullish",
        signal_type="gap_up",
        strength=8,
        entry_price=100.0,
        shares=10,
    )
    assert trade["status"] == "open"

    listed = await paper_trading.list_paper_trades(status="open")
    assert listed["count"] == 1
    assert listed["trades"][0]["trade_id"] == trade["trade_id"]

    closed = await paper_trading.close_paper_trade(
        trade["trade_id"],
        exit_price=110.0,
        exit_reason="target_hit",
    )
    assert closed is not None
    assert closed["status"] == "closed"
    assert closed["pnl_pct"] == pytest.approx(10.0)
    assert closed["pnl_amount"] == pytest.approx(100.0)

    summary = await paper_trading.paper_trade_summary()
    assert summary["closed"] == 1
    assert summary["wins"] == 1
    assert summary["win_rate"] == 100.0


@pytest.mark.asyncio
async def test_close_paper_trade_records_signal_outcome(paper_db):
    """Closing a paper trade must persist a signal_outcomes row so the weekly
    learners train on real paper-trade P&L (the self-correction wiring)."""
    trade = await paper_trading.create_paper_trade(
        symbol="TCS", direction="bullish", signal_type="macd_divergence",
        strength=7, entry_price=100.0, stop_loss=95.0, target=115.0,
        entry_date="2026-05-20", shares=10, position_size=1000.0, source="auto",
    )
    tid = trade["trade_id"]
    # Close above entry → a win, +10%.
    await paper_trading.close_paper_trade(tid, exit_price=110.0, exit_date="2026-05-25")

    con = sqlite3.connect(paper_db)
    row = con.execute(
        "SELECT signal_type, direction, pnl_pct, outcome, hold_days "
        "FROM signal_outcomes WHERE signal_id=?", (f"paper_{tid}",),
    ).fetchone()
    con.close()
    assert row is not None
    assert row[0] == "macd_divergence"
    assert row[1] == "bullish"
    assert row[2] == pytest.approx(10.0, abs=0.01)
    assert row[3] == "win"
    assert row[4] == 5  # 2026-05-20 → 2026-05-25


@pytest.mark.asyncio
async def test_close_records_loss_outcome(paper_db):
    trade = await paper_trading.create_paper_trade(
        symbol="INFY", direction="bullish", signal_type="double_top",
        strength=5, entry_price=100.0, stop_loss=95.0, target=115.0,
        entry_date="2026-05-20", shares=10, source="auto",
    )
    tid = trade["trade_id"]
    await paper_trading.close_paper_trade(tid, exit_price=92.0, exit_date="2026-05-22")
    con = sqlite3.connect(paper_db)
    row = con.execute(
        "SELECT pnl_pct, outcome FROM signal_outcomes WHERE signal_id=?",
        (f"paper_{tid}",),
    ).fetchone()
    con.close()
    assert row is not None
    assert row[0] < 0
    assert row[1] == "loss"

from __future__ import annotations
"""Guardrails added after the 0-for-33 all-short investigation:

  • create_paper_trade dedups OPEN positions across sources (a logical
    position = symbol + direction + entry_price), so the same trade can't be
    double-counted via the API and the legacy CSV import.
  • auto_open_from_recommendations refuses to deepen an already-lopsided book
    (directional-concentration cap), so the engine can't lock 100% short.
"""
import os
import sqlite3
import tempfile

import pytest

from app.database import CREATE_PAPER_TRADES_TABLE
from app.services import auto_paper_trader, paper_trading


@pytest.fixture
def wired_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute(CREATE_PAPER_TRADES_TABLE)
    con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('paper_capital', '100000')")
    con.commit()
    con.close()
    monkeypatch.setattr(auto_paper_trader, "DB_PATH", path)
    monkeypatch.setattr(paper_trading, "DB_PATH", path)
    yield path
    os.unlink(path)


def _seed_open(path, symbol, direction, entry, *, source="auto"):
    con = sqlite3.connect(path)
    con.execute(
        """INSERT INTO paper_trades
           (trade_id, symbol, direction, signal_type, strength, entry_price,
            entry_date, status, source)
           VALUES (?, ?, ?, 'multi_factor_engine', 5, ?, '2026-06-20', 'open', ?)""",
        (f"{symbol}{direction}"[:12], symbol, direction, entry, source),
    )
    con.commit()
    con.close()


def _rec(symbol, action, conviction, entry, stop, target, rr, sector="IT"):
    return {
        "symbol": symbol, "action": action, "conviction": conviction,
        "risk_reward": rr, "entry": entry, "stoploss": stop,
        "target1": target, "sector": sector,
    }


# ── dedup guard ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_paper_trade_dedups_open_position(wired_db):
    first = await paper_trading.create_paper_trade(
        symbol="ASHOKLEY", direction="bullish", signal_type="x",
        strength=5, entry_price=157.7, shares=211, source="api",
    )
    # Same logical position arriving again via a different source (e.g. CSV
    # import) must NOT create a second row — returns the existing trade.
    second = await paper_trading.create_paper_trade(
        symbol="ASHOKLEY", direction="bullish", signal_type="x",
        strength=5, entry_price=157.7, source="csv_import",
    )
    assert second["trade_id"] == first["trade_id"]

    con = sqlite3.connect(wired_db)
    n = con.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE symbol='ASHOKLEY' AND status='open'"
    ).fetchone()[0]
    con.close()
    assert n == 1


@pytest.mark.asyncio
async def test_create_paper_trade_allows_distinct_entry(wired_db):
    a = await paper_trading.create_paper_trade(
        symbol="WIPRO", direction="bearish", signal_type="x",
        strength=5, entry_price=178.9, source="api",
    )
    b = await paper_trading.create_paper_trade(
        symbol="WIPRO", direction="bearish", signal_type="x",
        strength=5, entry_price=198.4, source="api",
    )
    assert a["trade_id"] != b["trade_id"]
    con = sqlite3.connect(wired_db)
    n = con.execute("SELECT COUNT(*) FROM paper_trades WHERE symbol='WIPRO'").fetchone()[0]
    con.close()
    assert n == 2


# ── directional-concentration guardrail ───────────────────────────────────

@pytest.mark.asyncio
async def test_concentration_blocks_overweight_side(wired_db):
    # Book already 3-short. A 4th short would be 100% > 80% cap → blocked
    # before Kelly/gate even runs.
    for i, sym in enumerate(("TCS", "INFY", "HCLTECH")):
        _seed_open(wired_db, sym, "bearish", 100 + i)
    rec = _rec("WIPRO", "SELL", conviction=80, entry=100, stop=105, target=85, rr=3.0)
    result = await auto_paper_trader.auto_open_from_recommendations([rec], min_conviction=65)
    assert result["opened"] == []
    assert result["skipped_reason_counts"].get("directional_concentration_cap", 0) == 1


@pytest.mark.asyncio
async def test_concentration_allows_balancing_side(wired_db):
    # Same 3-short book, but a BUY *rebalances* it (1 long / 4 total = 25%),
    # so the concentration cap must NOT block it.
    for i, sym in enumerate(("TCS", "INFY", "HCLTECH")):
        _seed_open(wired_db, sym, "bearish", 100 + i)
    rec = _rec("RELIANCE", "BUY", conviction=80, entry=100, stop=95, target=115, rr=3.0)
    result = await auto_paper_trader.auto_open_from_recommendations([rec], min_conviction=65)
    assert result["skipped_reason_counts"].get("directional_concentration_cap", 0) == 0


@pytest.mark.asyncio
async def test_concentration_disabled_when_cap_ge_one(wired_db):
    for i, sym in enumerate(("TCS", "INFY", "HCLTECH")):
        _seed_open(wired_db, sym, "bearish", 100 + i)
    rec = _rec("WIPRO", "SELL", conviction=80, entry=100, stop=105, target=85, rr=3.0)
    result = await auto_paper_trader.auto_open_from_recommendations(
        [rec], min_conviction=65, max_directional_concentration=1.0,
    )
    # Cap disabled → not blocked for concentration (may still open or skip for
    # other reasons, but never for the directional cap).
    assert result["skipped_reason_counts"].get("directional_concentration_cap", 0) == 0

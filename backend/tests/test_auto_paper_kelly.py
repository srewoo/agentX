from __future__ import annotations
"""Integration test: Kelly sizing inside auto_open_from_recommendations.

Verifies the two behaviours that make the sizing engine a money guard:
  • a negative-edge recommendation (poor reward:risk) is SKIPPED, not opened,
  • a positive-edge recommendation is opened WITH a Kelly-sized share count
    and position value (previously both were left null).
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
    # The auto-trader and paper_trading both reference DB_PATH in their own
    # namespace — patch both so every query hits the temp DB.
    monkeypatch.setattr(auto_paper_trader, "DB_PATH", path)
    monkeypatch.setattr(paper_trading, "DB_PATH", path)
    yield path
    os.unlink(path)


def _rec(symbol, action, conviction, entry, stop, target, rr, sector="IT"):
    return {
        "symbol": symbol, "action": action, "conviction": conviction,
        "risk_reward": rr, "entry": entry, "stoploss": stop,
        "target1": target, "sector": sector,
    }


@pytest.mark.asyncio
async def test_negative_edge_rec_is_skipped(wired_db):
    # b = reward/risk = 5/10 = 0.5 → non-positive Kelly at any sane p → skip.
    recs = [_rec("WIPRO", "BUY", conviction=70, entry=100, stop=90, target=105, rr=0.5)]
    result = await auto_paper_trader.auto_open_from_recommendations(recs, min_conviction=65)
    assert result["opened"] == []
    assert result["skipped_reason_counts"].get("negative_kelly_edge", 0) == 1


@pytest.mark.asyncio
async def test_positive_edge_rec_opens_with_kelly_size(wired_db):
    # b = 15/5 = 3.0, conviction 80 → positive Kelly → opens with real size.
    recs = [_rec("INFY", "BUY", conviction=80, entry=100, stop=95, target=115, rr=3.0)]
    result = await auto_paper_trader.auto_open_from_recommendations(recs, min_conviction=65)
    assert len(result["opened"]) == 1
    opened = result["opened"][0]
    assert opened["symbol"] == "INFY"
    assert opened["shares"] is not None and opened["shares"] > 0
    assert opened["position_size"] is not None and opened["position_size"] > 0
    # Hard 5% position cap on ₹100k @ ₹100 ⇒ ≤ 50 shares.
    assert opened["shares"] <= 50

    # And it was actually persisted with the sized shares.
    con = sqlite3.connect(wired_db)
    row = con.execute(
        "SELECT shares, position_size FROM paper_trades WHERE symbol='INFY'"
    ).fetchone()
    con.close()
    assert row is not None
    assert row[0] == opened["shares"]


@pytest.mark.asyncio
async def test_bad_data_quality_rec_is_rejected_by_gate(wired_db):
    # Positive-edge geometry, but the feed flagged the data as stale → the
    # risk gate must reject it (proves the quality gate is wired live).
    rec = _rec("INFY", "BUY", conviction=80, entry=100, stop=95, target=115, rr=3.0)
    rec["data_quality"] = "stale"
    result = await auto_paper_trader.auto_open_from_recommendations([rec], min_conviction=65)
    assert result["opened"] == []
    assert result["skipped_reason_counts"].get("risk_gate_rejected", 0) == 1


@pytest.mark.asyncio
async def test_earnings_blackout_rec_is_rejected_by_gate(wired_db, monkeypatch):
    # FMP says INFY reports within the blackout window → gate must reject,
    # proving the FMP earnings calendar is wired into the live gate path.
    from app.services import fmp_fetcher

    async def _blackout(symbol, **kwargs):
        return True
    monkeypatch.setattr(fmp_fetcher, "is_in_earnings_blackout", _blackout)

    rec = _rec("INFY", "BUY", conviction=80, entry=100, stop=95, target=115, rr=3.0)
    result = await auto_paper_trader.auto_open_from_recommendations([rec], min_conviction=65)
    assert result["opened"] == []
    assert result["skipped_reason_counts"].get("risk_gate_rejected", 0) == 1


# ── B1: meta-label probability threaded into Kelly sizing ──
# _effective_win_prob is the pure decision point — ONE probability source,
# used once. A deployed meta-label p(win) IS the sizing probability (clamped
# to the conservative bounds); the conviction heuristic applies only when no
# measured probability exists. The old min(conv_p, meta_p) double-counted a
# weak p(win): the meta gate had already scaled conviction upstream.

def test_effective_win_prob_no_meta_label_is_conviction_only():
    # No deployed meta-label model → identical to the old conviction map.
    conv = 80.0
    assert auto_paper_trader._effective_win_prob(conv, None) == \
        auto_paper_trader._win_probability(conv)


def test_effective_win_prob_weak_meta_label_shrinks_the_bet():
    # A pessimistic measured p(win) must drag the sizing probability DOWN
    # below the conviction-only value — conviction never overrides a weak
    # measured edge.
    conv = 90.0
    conv_only = auto_paper_trader._win_probability(conv)
    eff = auto_paper_trader._effective_win_prob(conv, meta_label_prob=0.40)
    assert eff == 0.40
    assert eff < conv_only


def test_effective_win_prob_is_not_double_shrunk_by_conviction():
    # Regression for the double-count: meta_p already scaled conviction in the
    # recommendation meta gate, so sizing must use meta_p directly — the same
    # measured probability regardless of the (already-scaled) conviction.
    eff_low_conv = auto_paper_trader._effective_win_prob(40.0, meta_label_prob=0.52)
    eff_high_conv = auto_paper_trader._effective_win_prob(95.0, meta_label_prob=0.52)
    assert eff_low_conv == eff_high_conv == 0.52


def test_effective_win_prob_strong_meta_label_cannot_inflate_beyond_cap():
    # A bullish measured p(win) must NOT push sizing above the conservative
    # global cap — no probability source may imply more edge than measured.
    eff = auto_paper_trader._effective_win_prob(70.0, meta_label_prob=0.95)
    assert eff == auto_paper_trader._WIN_PROB_CAP


def test_effective_win_prob_clamps_out_of_range_meta_label():
    # Defensive: a malformed p(win) outside [0,1] is clamped — capped above,
    # floored at 0 (a garbage-low p must yield a guaranteed Kelly skip, never
    # be floored UP into a placeable bet).
    assert auto_paper_trader._effective_win_prob(60.0, 1.7) == \
        auto_paper_trader._WIN_PROB_CAP
    assert auto_paper_trader._effective_win_prob(60.0, -0.2) == 0.0

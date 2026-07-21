from __future__ import annotations
"""4.3 — shadow sample of rejected signals: deterministic sampler, sim, bias bound."""
import os
import sqlite3
import tempfile

import pytest

from app.database import CREATE_PAPER_TRADES_TABLE
from app.services import shadow_sample as ss


# ── deterministic sampler ──
def test_should_sample_is_deterministic():
    key = "INFY|bullish|2026-06-01|negative_kelly_edge"
    assert ss.should_sample(key) == ss.should_sample(key)   # stable


def test_should_sample_rate_bounds():
    assert ss.should_sample("x", rate=0.0) is False
    assert ss.should_sample("x", rate=1.0) is True


def test_should_sample_fraction_approximates_rate():
    keys = [f"SYM{i}|bullish|2026-06-01|r" for i in range(2000)]
    hit = sum(1 for k in keys if ss.should_sample(k, rate=0.05))
    assert 0.03 < hit / len(keys) < 0.07     # ~5%


# ── outcome simulation (gap-aware, reuses backtest exit model) ──
def test_simulate_shadow_outcome_target_hit():
    bars = [{"open": 101, "high": 116, "low": 100, "close": 115}]   # gaps toward target
    out = ss.simulate_shadow_outcome("bullish", 100, 95, 115, bars)
    assert out["exit_reason"] == "target"
    assert out["pnl_pct"] == pytest.approx(15.0, abs=1e-6)


def test_simulate_shadow_outcome_none_without_bars():
    assert ss.simulate_shadow_outcome("bullish", 100, 95, 115, []) is None


# ── bias bound ──
def test_bias_bound_detects_discarded_winners():
    b = ss.bias_bound(taken_win_rate=0.50, shadow_win_rate=0.62)
    assert b["discarded_edge_gap"] == pytest.approx(0.12)
    assert "discarded winners" in b["interpretation"]


def test_bias_bound_funnel_sheds_weak_setups():
    b = ss.bias_bound(taken_win_rate=0.55, shadow_win_rate=0.40)
    assert b["discarded_edge_gap"] < 0
    assert "correctly shed" in b["interpretation"]


# ── store + report ──
@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute(CREATE_PAPER_TRADES_TABLE)
    con.commit(); con.close()
    yield path
    os.unlink(path)


@pytest.mark.asyncio
async def test_log_shadow_reject_respects_sample(db):
    # rate=1.0 always logs; rate=0.0 never logs.
    logged = await ss.log_shadow_reject(
        symbol="INFY", direction="bullish", entry=100, stop=95, target=115,
        reason="negative_kelly_edge", rate=1.0, db_path=db)
    assert logged is True
    not_logged = await ss.log_shadow_reject(
        symbol="TCS", direction="bullish", entry=100, stop=95, target=115,
        reason="negative_kelly_edge", rate=0.0, db_path=db)
    assert not_logged is False


@pytest.mark.asyncio
async def test_bias_report_runs_on_empty(db):
    rep = await ss.bias_report(db_path=db)
    assert rep["taken_n"] == 0 and rep["shadow_n"] == 0
    assert "caveat" in rep

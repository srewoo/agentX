"""Tests for the recommendation self-improvement loop.

We exercise:
  - store_recommendation persists directional recs and skips HOLD/AVOID
  - factor_edge_multiplier returns sane values from the in-memory cache
  - _recalculate_factor_performance correctly derives per-factor edge

The cron path (`evaluate_recommendation_outcomes`) hits NSE/yfinance and is
covered by integration tests, not here.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.models.recommendation import Recommendation, SignalContribution


def _make_rec(symbol: str = "PNB", action: str = "BUY", conv: int = 70) -> Recommendation:
    """Minimal valid Recommendation for tracker tests."""
    sigs = [
        SignalContribution(name="trend",          weight=0.16, value=1.0,  score=0.6, direction="bullish"),
        SignalContribution(name="momentum",       weight=0.12, value=60.0, score=0.4, direction="bullish"),
        SignalContribution(name="news_sentiment", weight=0.06, value=0.1,  score=0.1, direction="neutral"),
    ]
    return Recommendation(
        symbol=symbol, exchange="NSE", horizon="swing", action=action,
        conviction=conv, entry=100.0, stoploss=95.0, target1=110.0, target2=115.0,
        risk_reward=2.0, timeframe_days=10, signals=sigs,
        reasons=["test"], sector="Banking", market_cap_band="LARGE",
        last_price=100.0, price_change_pct_1d=0.5,
        delivery_pct=None, fii_dii_signal=None, f_and_o_signal=None,
        generated_at=datetime.now(timezone.utc),
    )


# ── isolated DB fixture ─────────────────────────────────────────────────

@pytest.fixture
def isolated_db(monkeypatch):
    """Point DB_PATH at a fresh tempfile so tests don't touch stockpilot.db."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    # The tracker reads DB_PATH at call time, so monkey-patching the
    # module attribute is enough.
    from app.services import recommendation_tracker as tracker_mod
    monkeypatch.setattr(tracker_mod, "DB_PATH", path)

    # Bootstrap schema.
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE recommendation_outcomes (
            rec_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, horizon TEXT NOT NULL,
            action TEXT NOT NULL, conviction INTEGER NOT NULL,
            entry REAL NOT NULL, stoploss REAL NOT NULL, target1 REAL NOT NULL,
            timeframe_days INTEGER NOT NULL, signals_json TEXT NOT NULL,
            sector TEXT, created_at TEXT NOT NULL,
            outcome TEXT, exit_price REAL, exit_time TEXT,
            pnl_pct REAL, evaluated_at TEXT
        );
        CREATE TABLE factor_performance (
            factor TEXT PRIMARY KEY,
            total_directional INTEGER DEFAULT 0,
            aligned_count INTEGER DEFAULT 0,
            aligned_avg_pnl REAL DEFAULT 0,
            overall_avg_pnl REAL DEFAULT 0,
            edge REAL DEFAULT 0,
            updated_at TEXT
        );
    """)
    con.commit()
    con.close()
    yield path
    os.unlink(path)


# ── store_recommendation ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_store_persists_directional(isolated_db):
    from app.services.recommendation_tracker import store_recommendation
    await store_recommendation(_make_rec(action="BUY"))
    con = sqlite3.connect(isolated_db)
    rows = con.execute("SELECT symbol, action, signals_json FROM recommendation_outcomes").fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "BUY"
    sigs = json.loads(rows[0][2])
    assert {s["name"] for s in sigs} >= {"trend", "momentum", "news_sentiment"}


@pytest.mark.asyncio
async def test_store_skips_hold(isolated_db):
    from app.services.recommendation_tracker import store_recommendation
    await store_recommendation(_make_rec(action="HOLD"))
    con = sqlite3.connect(isolated_db)
    assert con.execute("SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_store_skips_none(isolated_db):
    from app.services.recommendation_tracker import store_recommendation
    await store_recommendation(None)  # type: ignore[arg-type]
    con = sqlite3.connect(isolated_db)
    assert con.execute("SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_store_is_idempotent_for_same_id(isolated_db):
    """Same (symbol, horizon, generated_at) shouldn't double-insert."""
    from app.services.recommendation_tracker import store_recommendation
    rec = _make_rec()
    await store_recommendation(rec)
    await store_recommendation(rec)  # same rec_id
    con = sqlite3.connect(isolated_db)
    assert con.execute("SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0] == 1


# ── factor edge math ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recalc_derives_edge_from_outcomes(isolated_db):
    """Insert resolved outcomes and check the derived edge per factor."""
    from app.services import recommendation_tracker as tracker_mod
    from app.services.recommendation_tracker import _recalculate_factor_performance

    con = sqlite3.connect(isolated_db)
    # Two BUY outcomes:
    #  - WIN at +5%, "trend" was strongly aligned (score 0.6)
    #  - LOSS at -3%, "trend" was misaligned (score -0.5)
    # → "trend" aligned PnL = +5; overall avg = (5 + -3)/2 = 1.0;
    # → edge = +4pp.
    rows = [
        ("r1", "PNB", "swing", "BUY", 70, 100, 95, 110, 10,
         json.dumps([{"name": "trend", "score": 0.6, "weight": 0.16, "direction": "bullish"}]),
         "Banking", "2026-01-01T00:00:00+00:00",
         "win", 110.0, "2026-01-05", 5.0, "2026-01-06"),
        ("r2", "RELIANCE", "swing", "BUY", 60, 200, 190, 220, 10,
         json.dumps([{"name": "trend", "score": -0.5, "weight": 0.16, "direction": "bearish"}]),
         "Energy", "2026-01-02T00:00:00+00:00",
         "loss", 190.0, "2026-01-06", -3.0, "2026-01-07"),
    ]
    con.executemany("""
        INSERT INTO recommendation_outcomes
        (rec_id, symbol, horizon, action, conviction, entry, stoploss, target1,
         timeframe_days, signals_json, sector, created_at,
         outcome, exit_price, exit_time, pnl_pct, evaluated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, rows)
    con.commit()
    con.close()

    await _recalculate_factor_performance()

    # Cache should now contain "trend" with edge ≈ +4 pp.
    assert "trend" in tracker_mod._factor_edge_cache
    edge = tracker_mod._factor_edge_cache["trend"]
    assert edge == pytest.approx(4.0, abs=0.5)


@pytest.mark.asyncio
async def test_recalc_no_op_with_no_outcomes(isolated_db):
    from app.services import recommendation_tracker as tracker_mod
    from app.services.recommendation_tracker import _recalculate_factor_performance
    tracker_mod._factor_edge_cache.clear()
    await _recalculate_factor_performance()
    assert tracker_mod._factor_edge_cache == {}


# ── factor_edge_multiplier ──────────────────────────────────────────────

class TestFactorEdgeMultiplier:
    def setup_method(self):
        from app.services import recommendation_tracker as t
        t._factor_edge_cache.clear()

    def test_unknown_factor_returns_one(self):
        from app.services.recommendation_tracker import factor_edge_multiplier
        assert factor_edge_multiplier("anything") == 1.0

    def test_positive_edge_boosts(self):
        from app.services import recommendation_tracker as t
        from app.services.recommendation_tracker import factor_edge_multiplier
        t._factor_edge_cache.update({"trend": 4.0})  # +4pp PnL edge
        m = factor_edge_multiplier("trend")
        assert 1.0 < m <= 1.5

    def test_negative_edge_suppresses(self):
        from app.services import recommendation_tracker as t
        from app.services.recommendation_tracker import factor_edge_multiplier
        t._factor_edge_cache.update({"momentum": -3.0})
        m = factor_edge_multiplier("momentum")
        assert 0.5 <= m < 1.0

    def test_clamps_to_range(self):
        from app.services import recommendation_tracker as t
        from app.services.recommendation_tracker import factor_edge_multiplier, _WEIGHT_MIN, _WEIGHT_MAX
        t._factor_edge_cache.update({"x": 100.0, "y": -100.0})
        assert factor_edge_multiplier("x") == _WEIGHT_MAX
        assert factor_edge_multiplier("y") == _WEIGHT_MIN

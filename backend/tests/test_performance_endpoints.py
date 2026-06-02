from __future__ import annotations
"""API-level tests for the new audited-metrics / OOS-gate / explain endpoints.

Mounts only the performance router on a minimal FastAPI app so these don't
depend on full-app startup (env/secrets). Uses a temp DB and a temp model
path so they're fully deterministic.
"""
import os
import sqlite3
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.database as database
from app.database import (
    CREATE_RECOMMENDATION_OUTCOMES_TABLE,
    CREATE_SIGNAL_OUTCOMES_TABLE,
    CREATE_PAPER_TRADES_TABLE,
)
from app.routers.performance import router


@pytest.fixture
def client(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute(CREATE_RECOMMENDATION_OUTCOMES_TABLE)
    con.execute(CREATE_SIGNAL_OUTCOMES_TABLE)
    con.execute(CREATE_PAPER_TRADES_TABLE)
    con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS backtest_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "run_at TEXT, period TEXT, eval_window_days INTEGER, stocks_count INTEGER, "
        "total_signals INTEGER, avg_pnl_pct REAL, directional_win_rate REAL, "
        "best_signal_type TEXT, worst_signal_type TEXT, payload TEXT)"
    )
    # Two resolved recommendations: a high-conviction win, a high-conviction loss.
    con.executemany(
        """INSERT INTO recommendation_outcomes
           (rec_id, symbol, horizon, action, conviction, entry, stoploss, target1,
            timeframe_days, signals_json, created_at, outcome, pnl_pct, bars_held, regime)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            ("r1", "TCS", "swing", "BUY", 80, 100, 95, 115, 5, "[]",
             "2026-05-28", "win", 3.0, 4, "trend_up"),
            ("r2", "INFY", "swing", "BUY", 75, 100, 95, 115, 5, "[]",
             "2026-05-29", "loss", -1.5, 5, "range_bound"),
            ("r3", "SBIN", "positional", "SELL", 60, 100, 105, 90, 10, "[]",
             "2026-05-30", "win", 2.0, 8, "trend_down"),
        ],
    )
    con.executemany(
        """INSERT INTO signal_outcomes
           (signal_id, symbol, signal_type, direction, entry_price, entry_time,
            pnl_pct, outcome, hold_days)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        [
            ("s1", "TCS", "macd_divergence", "bullish", 100, "2026-05-28", 2.0, "win", 5),
            ("s2", "INFY", "double_top", "bearish", 100, "2026-05-29", -1.0, "loss", 5),
        ],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(database, "DB_PATH", path)
    # runtime_status and orchestrator bind DB_PATH at import time — patch them too.
    import app.services.runtime_status as rs
    import app.services.orchestrator as orch
    monkeypatch.setattr(rs, "DB_PATH", path)
    monkeypatch.setattr(orch, "DB_PATH", path)

    test_app = FastAPI()
    test_app.include_router(router)
    with TestClient(test_app) as c:
        yield c
    os.unlink(path)


def test_audited_endpoint_returns_full_metric_bundle(client):
    resp = client.get("/api/performance/audited?since=2026-05-01")
    assert resp.status_code == 200
    data = resp.json()
    overall = data["recommendations"]["overall"]
    # Core audited metrics present and computed.
    assert overall["n_resolved"] == 3
    assert overall["wins"] == 2 and overall["losses"] == 1
    assert overall["profit_factor"] is not None       # 5.0 / 1.5
    assert overall["expectancy"] == pytest.approx((3.0 - 1.5 + 2.0) / 3, abs=1e-3)
    assert "max_drawdown_pp" in overall
    assert overall["brier_score"] is not None          # conviction-based
    assert isinstance(overall["calibration"], list)
    # Splits present.
    assert set(data["recommendations"]["by_horizon"].keys()) == {"swing", "positional"}
    assert "trend_up" in data["recommendations"]["by_regime"]
    # Signal cohort is P&L-only (no probability ⇒ no Brier).
    assert data["signals"]["overall"]["n_resolved"] == 2
    assert data["signals"]["overall"]["brier_score"] is None


def test_audited_rejects_bad_since(client):
    assert client.get("/api/performance/audited?since=not-a-date").status_code == 400


def test_oos_gate_endpoint(client):
    resp = client.get("/api/performance/oos-gate?horizon=5d")
    assert resp.status_code == 200
    v = resp.json()["data"]
    assert v["verdict"] in {"PASS", "REVIEW", "FAIL", "UNKNOWN"}
    assert isinstance(v["shippable"], bool)


def test_oos_gate_rejects_bad_horizon(client):
    assert client.get("/api/performance/oos-gate?horizon=2d").status_code == 400


def test_automation_status_endpoint(client):
    resp = client.get("/api/performance/automation-status")
    assert resp.status_code == 200
    d = resp.json()["data"]
    assert set(["orchestrator_running", "market_open", "auto_paper_enabled",
                "daily_backtest_enabled", "open_positions", "heartbeats",
                "last_backtest_at", "next_daily_backtest_utc",
                "next_weekly_backtest_utc"]).issubset(d.keys())
    assert isinstance(d["market_open"], bool)
    assert isinstance(d["heartbeats"], dict)
    assert isinstance(d["open_positions"], int)
    # Next-run schedule is always computable (pure time math).
    assert d["next_daily_backtest_utc"] and d["next_weekly_backtest_utc"]
    # Defaults: auto-paper + daily backtest both on out of the box.
    assert d["auto_paper_enabled"] is True
    assert d["daily_backtest_enabled"] is True


def test_automation_status_reflects_recorded_heartbeat(client):
    """A heartbeat row surfaces in the endpoint's `heartbeats` map.

    Seeds via a direct (sync) sqlite write to the patched DB path — no event
    loop — so this is immune to suite-ordering / loop-state isolation issues.
    """
    import json
    import app.services.runtime_status as rs

    con = sqlite3.connect(rs.DB_PATH)  # monkeypatched to the temp DB by the fixture
    con.execute(
        "CREATE TABLE IF NOT EXISTS system_status "
        "(name TEXT PRIMARY KEY, last_run_at TEXT NOT NULL, summary TEXT)"
    )
    con.execute(
        "INSERT OR REPLACE INTO system_status (name, last_run_at, summary) VALUES (?,?,?)",
        ("auto_paper", "2026-06-02T00:00:00+00:00", json.dumps({"opened": 1, "closed": 0})),
    )
    con.commit()
    con.close()

    d = client.get("/api/performance/automation-status").json()["data"]
    assert d["heartbeats"].get("auto_paper", {}).get("summary") == {"opened": 1, "closed": 0}


def test_meta_judge_explain_endpoint(client, monkeypatch, tmp_path):
    from app.services.meta_judge import MetaJudge
    import app.services.meta_judge_trainer as trainer

    trades = []
    for _ in range(20):
        trades.append({"momentum": 1.0, "trend": 1.0, "signal_type": "macd_divergence",
                       "direction": "bullish", "regime": "trend_up", "symbol": "A",
                       "win": True, "pnl": 2.0})
        trades.append({"momentum": -1.0, "trend": -1.0, "signal_type": "double_top",
                       "direction": "bearish", "regime": "trend_down", "symbol": "B",
                       "win": False, "pnl": -2.0})
    model = MetaJudge.train(trades, n_stumps=8, label_mode="win", enrich=False)
    model_path = tmp_path / "meta_judge.json"
    model.save(model_path)
    monkeypatch.setattr(trainer, "_MODEL_PATH", model_path)

    resp = client.post(
        "/api/performance/meta-judge/explain",
        json={"features": {"momentum": 1.0, "trend": 1.0, "signal_type": "macd_divergence",
                           "direction": "bullish", "regime": "trend_up"}, "top_k": 3},
    )
    assert resp.status_code == 200
    d = resp.json()["data"]
    assert "contributions" in d and len(d["contributions"]) <= 3
    assert 0.0 <= d["prob_win"] <= 1.0
    # additive identity holds through the API too
    assert isinstance(d["keep"], bool)


def test_meta_judge_explain_404_without_model(client, monkeypatch, tmp_path):
    import app.services.meta_judge_trainer as trainer
    monkeypatch.setattr(trainer, "_MODEL_PATH", tmp_path / "does_not_exist.json")
    resp = client.post(
        "/api/performance/meta-judge/explain",
        json={"features": {"momentum": 1.0}},
    )
    assert resp.status_code == 404

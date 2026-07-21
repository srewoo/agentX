from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from app.services import recommendation_calibration as cal


def _trend_df(n: int = 180) -> pd.DataFrame:
    idx = pd.date_range(end=datetime.now(timezone.utc), periods=n, freq="D")
    close = np.linspace(100, 180, n)
    return pd.DataFrame(
        {
            "Open": close - 0.5,
            "High": close + 2.0,
            "Low": close - 2.0,
            "Close": close,
            "Volume": np.full(n, 1_000_000),
        },
        index=idx,
    )


@pytest.fixture
def calibration_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE backtest_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT NOT NULL,
            period TEXT NOT NULL,
            eval_window_days INTEGER NOT NULL,
            stocks_count INTEGER NOT NULL,
            total_signals INTEGER NOT NULL,
            avg_pnl_pct REAL,
            directional_win_rate REAL,
            best_signal_type TEXT,
            worst_signal_type TEXT,
            payload TEXT NOT NULL
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
        """
    )
    con.close()

    from app.services import recommendation_tracker as tracker

    monkeypatch.setattr(cal, "DB_PATH", path)
    monkeypatch.setattr(tracker, "DB_PATH", path)
    tracker._factor_edge_cache.clear()
    yield path
    os.unlink(path)


def test_calibration_universe_sizes_are_deterministic():
    assert len(cal.calibration_universe("nifty50")) <= 50
    assert len(cal.calibration_universe("nifty100", limit=3)) == 3
    assert "^NSEI" not in cal.calibration_universe("curated")


@pytest.mark.asyncio
async def test_large_scale_calibration_persists_run_and_factor_edges(
    calibration_db, monkeypatch
):
    async def fake_history(symbol: str, period: str, interval: str):
        assert interval == "1d"
        return _trend_df()

    monkeypatch.setattr(cal, "async_fetch_history", fake_history)

    result = await cal.run_large_scale_calibration(
        universe="nifty50",
        horizons=["swing"],
        period="1y",
        max_symbols=2,
        stride=20,
        concurrency=1,
        apply=True,
    )

    assert result["summary"]["total"] > 0
    assert "by_sector" in result
    assert "by_regime" in result
    assert result["factor_edges"]
    # Every factor-edge row now carries a significance p-value and an FDR
    # verdict (the multiple-comparisons gate).
    for row in result["factor_edges"]:
        assert "p_value" in row and 0.0 <= row["p_value"] <= 1.0
        assert "significant" in row

    con = sqlite3.connect(calibration_db)
    runs = con.execute("SELECT total_signals, payload FROM backtest_runs").fetchall()
    factors = con.execute("SELECT factor, edge FROM factor_performance").fetchall()
    con.close()

    assert runs and runs[0][0] == result["summary"]["total"]
    # FDR gate: ONLY significant factor edges are persisted to
    # factor_performance (and thus allowed to move a live weight multiplier).
    # The persisted count must equal the number of significant edges in the
    # payload — never the raw ranked list.
    combined = [*result["factor_edges"], *result.get("contextual_factor_edges", [])]
    n_significant = sum(1 for r in combined if r.get("significant"))
    assert len(factors) == n_significant

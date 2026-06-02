from __future__ import annotations
"""Tests for meta_judge_trainer.train_and_save — esp. the AUC-into-train_meta
fix that makes the orchestrator's deploy gate actually work."""
import os
import sqlite3
import tempfile

import pytest

from app.services import meta_judge_trainer
from app.services.meta_judge import MetaJudge


@pytest.fixture
def trainer_env(monkeypatch):
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE signal_outcomes (signal_id TEXT PRIMARY KEY, symbol TEXT, "
        "signal_type TEXT, direction TEXT, entry_price REAL, exit_price REAL, "
        "entry_time TEXT, exit_time TEXT, pnl_pct REAL, outcome TEXT, hold_days INTEGER, "
        "evaluated_at TEXT)"
    )
    # 240 separable rows: 'good' setups win, 'bad' setups lose → high AUC.
    rows = []
    for i in range(240):
        good = i % 2 == 0
        rows.append((
            f"s{i}", "TCS",
            "macd_divergence" if good else "double_top",
            "bullish" if good else "bearish",
            100.0, 103.0 if good else 98.0,
            f"2026-01-{(i % 27) + 1:02d}T00:00:00+00:00", None,
            2.0 if good else -2.0,
            "win" if good else "loss", 5, None,
        ))
    con.executemany(
        "INSERT INTO signal_outcomes (signal_id, symbol, signal_type, direction, "
        "entry_price, exit_price, entry_time, exit_time, pnl_pct, outcome, hold_days, "
        "evaluated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()

    import pathlib
    model_path = pathlib.Path(tempfile.mkdtemp()) / "meta_judge.json"
    monkeypatch.setattr(meta_judge_trainer, "DB_PATH", db)
    monkeypatch.setattr(meta_judge_trainer, "_MODEL_PATH", model_path)
    yield model_path
    os.unlink(db)


@pytest.mark.asyncio
async def test_train_and_save_injects_holdout_auc_into_train_meta(trainer_env):
    model_path = trainer_env
    result = await meta_judge_trainer.train_and_save(n_stumps=10)
    assert result["trained"] is True

    # The fix: the *saved model* (not just the sidecar) carries holdout AUC,
    # which is exactly what the orchestrator deploy gate reads.
    loaded = MetaJudge.load(model_path)
    auc = loaded.train_meta.get("holdout_metrics", {}).get("auc")
    assert auc is not None
    # Perfectly separable data ⇒ the gate (≥0.55) would deploy this model.
    assert auc >= 0.55


@pytest.mark.asyncio
async def test_insufficient_data_does_not_train(trainer_env, monkeypatch):
    # Raise the threshold above the row count → no training.
    result = await meta_judge_trainer.train_and_save(min_trades=10_000)
    assert result["trained"] is False
    assert "insufficient" in result["reason"].lower()

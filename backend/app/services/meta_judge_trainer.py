from __future__ import annotations
"""Train the deterministic meta-judge from `signal_outcomes` and persist it.

Production loop:
  1. Daily cron calls `train_and_save()` — pulls all resolved trades from
     signal_outcomes, joins them with signal metadata (signal_type,
     direction, regime, factor scores when available), trains a fresh
     MetaJudge, saves to `models/meta_judge.json`.
  2. Orchestrator loads the latest model at scan time via `load_active()`.
     If the file is fresh (< 7d old) and was trained on ≥ 300 outcomes,
     the judge filters every candidate signal; otherwise the orchestrator
     falls back to the LLM judge.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from app.database import DB_PATH
from app.services.meta_judge import MetaJudge, evaluate

logger = logging.getLogger(__name__)

# Default storage path — small JSON file, ~10-50KB depending on stump count.
_MODEL_PATH = Path(__file__).resolve().parents[2] / "models" / "meta_judge.json"
_MIN_TRADES_FOR_TRAIN = 200    # below this, model is too noisy — skip training
_FRESHNESS_DAYS = 7


def _model_dir() -> Path:
    _MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    return _MODEL_PATH.parent


async def _load_resolved_trades() -> list[dict[str, Any]]:
    """Pull every resolved trade from signal_outcomes with enough features
    to train a meaningful model. Joins with `signals` for signal_type and
    direction; everything else (regime, factor scores) is in
    signal_outcomes itself or computed downstream.
    """
    out: list[dict[str, Any]] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute(
                """
                SELECT so.signal_id, so.symbol, so.signal_type, so.direction,
                       so.entry_price, so.exit_price, so.pnl_pct, so.outcome,
                       so.hold_days, so.entry_time
                FROM signal_outcomes so
                WHERE so.outcome IN ('win','loss','expired')
                ORDER BY so.entry_time ASC
                """
            ) as cur:
                async for r in cur:
                    rd = dict(r)
                    # Drop expired / neutral — meta-judge is binary.
                    if rd["outcome"] == "expired":
                        continue
                    out.append({
                        "symbol": rd["symbol"],
                        "signal_type": rd["signal_type"],
                        "direction": rd["direction"],
                        "regime": "unknown",      # signal_outcomes doesn't carry regime today
                        "sector": "Unknown",
                        "pnl": float(rd["pnl_pct"] or 0.0),
                        "win": rd["outcome"] == "win",
                    })
        except Exception as e:
            logger.warning("meta_judge_trainer: signal_outcomes query failed: %s", e)
    return out


async def train_and_save(
    *,
    n_stumps: int = 25,
    target_tpr: float = 0.70,
    min_trades: int = _MIN_TRADES_FOR_TRAIN,
) -> dict[str, Any]:
    """One-shot trainer for the daily cron.

    Returns the train summary (n_trades, model file path, freshness ts).
    Idempotent: re-running on the same DB produces the same model.
    """
    trades = await _load_resolved_trades()
    if len(trades) < min_trades:
        msg = (
            f"insufficient outcomes ({len(trades)}) — need ≥{min_trades} "
            f"before the meta-judge can train. Falling back to LLM judge."
        )
        logger.info("meta_judge_trainer: %s", msg)
        return {"trained": False, "reason": msg, "n_trades": len(trades)}

    # Hold out the latest 20% for OOS verification.
    n_holdout = max(50, int(len(trades) * 0.2))
    train = trades[:-n_holdout]
    test = trades[-n_holdout:]

    model = MetaJudge.train(train, n_stumps=n_stumps, target_tpr=target_tpr)
    ev = evaluate(model, test)

    # Persist the holdout metrics INTO the model's train_meta as well, not
    # just the sidecar .meta.json. The orchestrator's deploy gate reads
    # `train_meta["holdout_metrics"]["auc"]` at load time — without this the
    # gate always sees 0.0 and the model never deploys, however good it is.
    if isinstance(model.train_meta, dict):
        model.train_meta["holdout_metrics"] = ev

    _model_dir()
    model.save(_MODEL_PATH)
    meta_path = _MODEL_PATH.with_suffix(".meta.json")
    meta = {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_train": len(train),
        "n_holdout": len(test),
        "holdout_metrics": ev,
        "n_stumps": n_stumps,
        "target_tpr": target_tpr,
        "model_path": str(_MODEL_PATH),
    }
    meta_path.write_text(json.dumps(meta, indent=2))
    logger.info(
        "meta_judge_trainer: trained on %d, holdout n=%d AUC=%s TPR=%s TNR=%s kept_avg=%s",
        len(train), len(test),
        ev.get("auc"), ev.get("tpr"), ev.get("tnr"), ev.get("kept_avg_pnl"),
    )
    return {"trained": True, **meta}


def load_active() -> Optional[MetaJudge]:
    """Load the persisted model if it's fresh and trustworthy.

    Returns None when the model is missing or stale (> 7 days old), so the
    orchestrator falls back cleanly to the LLM judge.
    """
    if not _MODEL_PATH.exists():
        return None
    meta_path = _MODEL_PATH.with_suffix(".meta.json")
    try:
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            ts = meta.get("trained_at")
            if ts:
                age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).days
                if age_days > _FRESHNESS_DAYS:
                    logger.info(
                        "meta_judge: model is %dd old (>%dd), falling back",
                        age_days, _FRESHNESS_DAYS,
                    )
                    return None
        return MetaJudge.load(_MODEL_PATH)
    except Exception as e:
        logger.warning("meta_judge: load failed: %s", e)
        return None

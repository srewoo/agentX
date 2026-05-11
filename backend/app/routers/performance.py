from __future__ import annotations
"""Performance tracking endpoints — win/loss stats for signal outcomes."""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.signal_edge import (
    EDGE_META,
    RECOMMENDED_MUTES,
    all_edge_rows,
)
from app.services.signal_tracker import (
    evaluate_signals,
    get_performance_stats,
    get_performance_summary,
    get_signal_accuracy,
)
from app.services.recommendation_tracker import (
    evaluate_recommendation_outcomes,
    get_factor_edge_snapshot,
)
from app.services.recommendation_calibration import run_large_scale_calibration
from app.services.paper_trading import (
    close_paper_trade,
    create_paper_trade,
    import_paper_trades_csv,
    list_paper_trades,
    paper_trade_summary,
)

router = APIRouter(prefix="/api/performance", tags=["performance"])
logger = logging.getLogger(__name__)

_CALIBRATION_JOBS: dict[str, dict[str, Any]] = {}
_MAX_CALIBRATION_JOBS = 20


class CreatePaperTradeRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    direction: str = Field(pattern="^(bullish|bearish)$")
    signal_type: str = Field(min_length=1, max_length=80)
    strength: int = Field(ge=1, le=10)
    entry_price: float = Field(gt=0)
    entry_date: Optional[str] = Field(default=None, max_length=40)
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    position_size: Optional[float] = None
    shares: Optional[int] = None
    trailing_stop: Optional[float] = None


class ClosePaperTradeRequest(BaseModel):
    exit_price: float = Field(gt=0)
    exit_date: Optional[str] = Field(default=None, max_length=40)
    exit_reason: str = Field(default="manual", max_length=80)


def _validate_calibration_request(
    universe: str,
    horizons: str,
    max_symbols: Optional[int],
    stride: int,
    concurrency: int,
    min_conviction: int,
) -> list[str]:
    allowed_universe = {"nifty50", "nifty100", "nifty500", "curated"}
    if universe not in allowed_universe:
        raise HTTPException(
            status_code=400,
            detail=f"universe must be one of {sorted(allowed_universe)}",
        )
    parsed_horizons = [h.strip() for h in horizons.split(",") if h.strip()]
    allowed_horizons = {"swing", "positional"}
    if not parsed_horizons or any(h not in allowed_horizons for h in parsed_horizons):
        raise HTTPException(status_code=400, detail="horizons must be swing,positional")
    if stride < 1 or stride > 30:
        raise HTTPException(status_code=400, detail="stride must be 1..30")
    if concurrency < 1 or concurrency > 10:
        raise HTTPException(status_code=400, detail="concurrency must be 1..10")
    if max_symbols is not None and (max_symbols < 1 or max_symbols > 500):
        raise HTTPException(status_code=400, detail="max_symbols must be 1..500")
    if min_conviction < 0 or min_conviction > 100:
        raise HTTPException(status_code=400, detail="min_conviction must be 0..100")
    return parsed_horizons


def _trim_calibration_jobs() -> None:
    if len(_CALIBRATION_JOBS) <= _MAX_CALIBRATION_JOBS:
        return
    completed = [
        (job_id, job)
        for job_id, job in _CALIBRATION_JOBS.items()
        if job.get("status") in {"completed", "failed"}
    ]
    completed.sort(key=lambda item: item[1].get("updated_at") or "")
    for job_id, _job in completed[: max(0, len(_CALIBRATION_JOBS) - _MAX_CALIBRATION_JOBS)]:
        _CALIBRATION_JOBS.pop(job_id, None)


async def _run_calibration_job(
    job_id: str,
    *,
    universe: str,
    horizons: list[str],
    period: Optional[str],
    max_symbols: Optional[int],
    stride: int,
    concurrency: int,
    min_conviction: int,
    apply: bool,
) -> None:
    job = _CALIBRATION_JOBS[job_id]
    now = datetime.now(timezone.utc).isoformat()
    job.update({"status": "running", "started_at": now, "updated_at": now})
    try:
        result = await run_large_scale_calibration(
            universe=universe,  # type: ignore[arg-type]
            horizons=horizons,  # type: ignore[arg-type]
            period=period,
            max_symbols=max_symbols,
            stride=stride,
            concurrency=concurrency,
            min_conviction=min_conviction,
            apply=apply,
        )
        now = datetime.now(timezone.utc).isoformat()
        job.update(
            {
                "status": "completed",
                "updated_at": now,
                "finished_at": now,
                "result": result,
                "summary": result.get("summary", {}),
            }
        )
    except Exception as exc:
        logger.exception("Recommendation calibration job %s failed: %s", job_id, exc)
        now = datetime.now(timezone.utc).isoformat()
        job.update(
            {
                "status": "failed",
                "updated_at": now,
                "finished_at": now,
                "error": str(exc),
            }
        )


@router.get("/edge")
async def signal_edge():
    """Static per-signal-type edge from the latest internal backtest run.

    Used by the extension to surface 'this setup historically wins X%' on
    each signal card and to default-mute unprofitable detectors.
    """
    return {
        "meta": EDGE_META,
        "recommended_mutes": RECOMMENDED_MUTES,
        "rows": all_edge_rows(),
    }


@router.get("/recommendation-calibration")
async def recommendation_calibration():
    """Current learned factor-edge multipliers for the recommendation engine."""
    snapshot = get_factor_edge_snapshot()
    return {
        "data": {
            "factors": snapshot,
            "sample_policy": "Factors are only seeded on startup after the minimum sample threshold; fresh runs are conservative.",
        }
    }


@router.get("/summary")
async def performance_summary():
    """Return overall win rate, avg PnL, and total signals evaluated."""
    try:
        summary = await get_performance_summary()
        return {"data": summary}
    except Exception as e:
        logger.error(f"Error fetching performance summary: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch performance summary")


@router.get("/by-type")
async def performance_by_type(
    signal_type: Optional[str] = None,
    direction: Optional[str] = None,
):
    """
    Return performance breakdown by signal_type.
    Optionally filter by signal_type and/or direction.
    """
    try:
        if signal_type and direction:
            accuracy = await get_signal_accuracy(signal_type, direction)
            return {"data": [accuracy]}

        stats = await get_performance_stats()

        # Apply optional filters
        if signal_type:
            stats = [s for s in stats if s["signal_type"] == signal_type]
        if direction:
            stats = [s for s in stats if s["direction"] == direction]

        return {"data": stats}
    except Exception as e:
        logger.error(f"Error fetching performance by type: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch performance stats")


@router.get("/backtest-history")
async def backtest_history(limit: int = 12):
    """Last N weekly autonomous backtest runs (chronological, newest first)."""
    import json
    import aiosqlite
    from app.database import DB_PATH
    rows: list[dict] = []
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, run_at, period, eval_window_days, stocks_count, total_signals, "
                "       avg_pnl_pct, directional_win_rate, best_signal_type, worst_signal_type "
                "FROM backtest_runs ORDER BY run_at DESC LIMIT ?",
                (max(1, min(limit, 100)),),
            ) as cur:
                async for r in cur:
                    rows.append(dict(r))
        return {"runs": rows, "count": len(rows)}
    except Exception as e:
        logger.error("backtest_history error: %s", e)
        return {"runs": [], "count": 0}


@router.get("/insights")
async def performance_insights():
    """Actionable insights from live perf vs static edge vs recent backtests.

    Compares:
      - The internal backtest baseline (signal_edge.SIGNAL_EDGE — 25k trades).
      - The user's live signal performance (signal_tracker._performance_cache).
      - The last weekly autonomous backtest (backtest_runs table).
    Outputs ranked, plain-English suggestions the UI can render and the user
    can one-click apply (mute, tune R:R, etc).
    """
    import json
    import aiosqlite
    from app.database import DB_PATH
    from app.services.signal_edge import SIGNAL_EDGE, RECOMMENDED_MUTES

    insights: list[dict] = []

    # 1) Drift: live cache vs baseline edge
    try:
        from app.services.signal_tracker import _performance_cache  # type: ignore[attr-defined]
        for key, live in _performance_cache.items():
            if not isinstance(live, dict):
                continue
            if (live.get("total_signals") or 0) < 20:
                continue  # too small to trust
            stype, _, direction = key.partition(":")
            edge = SIGNAL_EDGE.get((stype, direction))
            if not edge:
                continue
            live_wr = float(live.get("win_rate") or 0)
            base_wr = float(edge["win_rate"])
            delta = live_wr - base_wr
            if abs(delta) < 5:
                continue
            insights.append({
                "kind": "drift",
                "severity": "warn" if delta < -8 else ("good" if delta > 8 else "info"),
                "signal_type": stype,
                "direction": direction,
                "live_win_rate": round(live_wr, 1),
                "baseline_win_rate": round(base_wr, 1),
                "delta_pct": round(delta, 1),
                "sample_size": live.get("total_signals"),
                "title": (
                    f"{stype} ({direction}) is "
                    f"{'underperforming' if delta < 0 else 'outperforming'} "
                    f"baseline by {abs(delta):.1f}pp"
                ),
                "action": "mute" if delta < -8 else None,
                "action_label": "Mute this type" if delta < -8 else None,
            })
    except Exception as e:
        logger.debug("Live drift check failed: %s", e)

    # 2) Week-over-week regression in last two backtest runs
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT run_at, avg_pnl_pct, directional_win_rate, best_signal_type, worst_signal_type, payload "
                "FROM backtest_runs ORDER BY run_at DESC LIMIT 2"
            ) as cur:
                rows = [dict(r) async for r in cur]

        if len(rows) >= 2:
            cur_run, prev_run = rows[0], rows[1]
            wr_delta = (cur_run["directional_win_rate"] or 0) - (prev_run["directional_win_rate"] or 0)
            pnl_delta = (cur_run["avg_pnl_pct"] or 0) - (prev_run["avg_pnl_pct"] or 0)
            if abs(wr_delta) >= 3 or abs(pnl_delta) >= 0.2:
                insights.append({
                    "kind": "wow",
                    "severity": "warn" if (wr_delta < -3 or pnl_delta < -0.2) else "good",
                    "title": (
                        f"Week-over-week: WR {wr_delta:+.1f}pp, avg PnL {pnl_delta:+.2f}%"
                    ),
                    "current": {"wr": cur_run["directional_win_rate"], "pnl": cur_run["avg_pnl_pct"], "best": cur_run["best_signal_type"], "worst": cur_run["worst_signal_type"]},
                    "previous": {"wr": prev_run["directional_win_rate"], "pnl": prev_run["avg_pnl_pct"]},
                })
    except Exception as e:
        logger.debug("WoW comparison failed: %s", e)

    # 3) Static recommended mutes (always present as informational)
    insights.append({
        "kind": "recommended_mutes",
        "severity": "info",
        "title": f"{len(RECOMMENDED_MUTES)} signal types historically lose money",
        "signal_types": RECOMMENDED_MUTES,
        "action": "apply_mutes",
        "action_label": "Apply recommended mutes",
    })

    # Order: warn first, then good, then info
    sev_order = {"warn": 0, "good": 1, "info": 2}
    insights.sort(key=lambda i: sev_order.get(i.get("severity", "info"), 3))
    return {"insights": insights, "count": len(insights)}


@router.post("/evaluate")
async def trigger_evaluation():
    """Manually trigger evaluation of signal outcomes."""
    try:
        result = await evaluate_signals()
        return {"data": result, "message": "Signal evaluation complete"}
    except Exception as e:
        logger.error(f"Error during manual evaluation: {e}")
        raise HTTPException(status_code=500, detail="Failed to evaluate signals")


@router.post("/evaluate-recommendations")
async def trigger_recommendation_evaluation():
    """Manually evaluate stored BUY/SELL recommendation outcomes."""
    try:
        result = await evaluate_recommendation_outcomes()
        return {"data": result, "message": "Recommendation evaluation complete"}
    except Exception as e:
        logger.error("Error during recommendation evaluation: %s", e)
        raise HTTPException(status_code=500, detail="Failed to evaluate recommendations")


@router.post("/paper-trades/import-csv")
async def import_paper_trades():
    """Import legacy local paper_trades/trades.csv into SQLite."""
    csv_path = Path(__file__).resolve().parents[2] / "paper_trades" / "trades.csv"
    try:
        result = await import_paper_trades_csv(csv_path)
        return {"data": result, "message": "Paper trades imported"}
    except Exception as e:
        logger.exception("Paper trade import failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to import paper trades")


@router.get("/paper-trades")
async def get_paper_trades(
    status: Optional[str] = None,
    symbol: Optional[str] = None,
    limit: int = 100,
):
    try:
        return {"data": await list_paper_trades(status=status, symbol=symbol, limit=limit)}
    except Exception as e:
        logger.exception("Paper trade list failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to list paper trades")


@router.get("/paper-trades/summary")
async def get_paper_trade_summary():
    try:
        return {"data": await paper_trade_summary()}
    except Exception as e:
        logger.exception("Paper trade summary failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to summarize paper trades")


@router.post("/paper-trades", status_code=201)
async def post_paper_trade(body: CreatePaperTradeRequest):
    try:
        trade = await create_paper_trade(**body.model_dump())
        return {"data": trade, "message": "Paper trade created"}
    except Exception as e:
        logger.exception("Paper trade create failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to create paper trade")


@router.post("/paper-trades/{trade_id}/close")
async def post_close_paper_trade(trade_id: str, body: ClosePaperTradeRequest):
    try:
        trade = await close_paper_trade(trade_id, **body.model_dump())
        if not trade:
            raise HTTPException(status_code=404, detail="Paper trade not found")
        return {"data": trade, "message": "Paper trade closed"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Paper trade close failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to close paper trade")


@router.post("/calibrate-recommendations")
async def calibrate_recommendations(
    universe: str = "nifty100",
    horizons: str = "swing,positional",
    period: Optional[str] = None,
    max_symbols: Optional[int] = None,
    stride: int = 5,
    concurrency: int = 3,
    min_conviction: int = 0,
    apply: bool = True,
):
    """Run large-scale recommendation calibration over free EOD data.

    `universe`: nifty50 | nifty100 | nifty500 | curated. Nifty500 currently
    means the largest curated repo universe available locally.
    """
    parsed_horizons = _validate_calibration_request(
        universe, horizons, max_symbols, stride, concurrency, min_conviction,
    )

    try:
        result = await run_large_scale_calibration(
            universe=universe,  # type: ignore[arg-type]
            horizons=parsed_horizons,  # type: ignore[arg-type]
            period=period,
            max_symbols=max_symbols,
            stride=stride,
            concurrency=concurrency,
            min_conviction=min_conviction,
            apply=apply,
        )
        return {"data": result, "message": "Recommendation calibration complete"}
    except Exception as e:
        logger.exception("Recommendation calibration failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to calibrate recommendations")


@router.post("/calibrate-recommendations/jobs")
async def start_calibration_job(
    universe: str = "nifty100",
    horizons: str = "swing,positional",
    period: Optional[str] = None,
    max_symbols: Optional[int] = None,
    stride: int = 5,
    concurrency: int = 3,
    min_conviction: int = 0,
    apply: bool = True,
):
    """Start recommendation calibration in the background.

    Use this for Nifty100/Nifty500 or 5-year runs. The synchronous endpoint is
    still useful for small smoke tests, but the app-level request timeout is 60s.
    """
    parsed_horizons = _validate_calibration_request(
        universe, horizons, max_symbols, stride, concurrency, min_conviction,
    )
    running = [
        job_id
        for job_id, job in _CALIBRATION_JOBS.items()
        if job.get("status") == "running"
    ]
    if running:
        raise HTTPException(
            status_code=409,
            detail={"message": "A calibration job is already running", "job_id": running[0]},
        )

    _trim_calibration_jobs()
    job_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()
    _CALIBRATION_JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "created_at": now,
        "updated_at": now,
        "params": {
            "universe": universe,
            "horizons": parsed_horizons,
            "period": period,
            "max_symbols": max_symbols,
            "stride": stride,
            "concurrency": concurrency,
            "min_conviction": min_conviction,
            "apply": apply,
        },
    }
    task = asyncio.create_task(
        _run_calibration_job(
            job_id,
            universe=universe,
            horizons=parsed_horizons,
            period=period,
            max_symbols=max_symbols,
            stride=stride,
            concurrency=concurrency,
            min_conviction=min_conviction,
            apply=apply,
        )
    )
    _CALIBRATION_JOBS[job_id]["task"] = task
    return {
        "job_id": job_id,
        "status": "queued",
        "status_url": f"/api/performance/calibrate-recommendations/jobs/{job_id}",
        "message": "Recommendation calibration started",
    }


@router.get("/calibrate-recommendations/jobs/{job_id}")
async def get_calibration_job(job_id: str):
    job = _CALIBRATION_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Calibration job not found")
    return {
        key: value
        for key, value in job.items()
        if key != "task"
    }

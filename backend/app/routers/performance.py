from __future__ import annotations
"""Performance tracking endpoints — win/loss stats for signal outcomes."""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException

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

router = APIRouter(prefix="/api/performance", tags=["performance"])
logger = logging.getLogger(__name__)


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

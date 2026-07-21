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
    edge_freshness,
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
        "freshness": edge_freshness(),
        "recommended_mutes": RECOMMENDED_MUTES,
        "rows": all_edge_rows(),
    }


@router.get("/recommendation-calibration")
async def recommendation_calibration():
    """Current learned factor-edge multipliers for the recommendation engine."""
    snapshot = get_factor_edge_snapshot()
    from app.services.recommendation_tuner import get_learned_weights_snapshot
    return {
        "data": {
            "factors": snapshot,
            "learned_weights": get_learned_weights_snapshot(),
            "sample_policy": "Factors are only seeded on startup after the minimum sample threshold; fresh runs are conservative.",
        }
    }


@router.get("/calibration-curve")
async def get_conviction_calibration_curve():
    """Conviction→p(win) reliability curve + Brier score (C1).

    Returns the persisted isotonic calibration of conviction against realized
    win rate: the reliability-diagram points, the fitted curve, and Brier for
    raw vs calibrated probabilities (so "did calibration help?" is a number).
    Build/refresh it via POST /calibration-curve.
    """
    from app.services.calibration_curve import get_calibration_curve
    report = await get_calibration_curve()
    return {"data": report or {"status": "not_built"}}


@router.post("/calibration-curve")
async def build_conviction_calibration_curve():
    """Fit + persist the conviction calibration curve from resolved outcomes.

    Requires ≥100 resolved BUY/SELL outcomes; below that returns
    `insufficient_data` and persists nothing (a curve from a tiny sample is
    noise).
    """
    from app.services.calibration_curve import build_calibration_curve
    return {"data": await build_calibration_curve()}


@router.get("/forward-performance")
async def forward_performance_report(benchmark_return_pct: Optional[float] = None):
    """Benchmark-relative forward performance + readiness (D2/D3).

    Forward expectancy, win rate (with Wilson CI), per-trade Sharpe, max
    drawdown, and alpha vs an optional benchmark return — from closed paper
    trades. `readiness` flags whether the sample is large enough to trust.
    """
    from app.services.forward_report import forward_performance
    return {"data": await forward_performance(benchmark_return_pct=benchmark_return_pct)}


@router.get("/scorecard")
async def north_star_scorecard():
    """The north-star scorecard — the ONE view every decision should key off.

    Leads with cost-adjusted, benchmark-EXCESS expectancy per trade and its 95%
    lower bound (is the edge above zero *with confidence*?), the calibration
    Brier score (can we trust the stated probabilities?), and forward-trade
    progress toward the 300-trade proof bar. Win rate is intentionally demoted
    to a supporting stat — it is not the objective.
    """
    from app.services.forward_report import forward_performance, DEFAULT_TARGET_TRADES
    from app.services.calibration_curve import build_calibration_curve

    perf = await forward_performance()
    bench = perf.get("benchmark") or {}

    try:
        calib = await build_calibration_curve()
        brier = calib.get("brier_calibrated")
    except Exception as e:
        logger.debug("scorecard: calibration curve failed: %s", e)
        brier = None

    n = int(perf.get("trades", 0))
    target = DEFAULT_TARGET_TRADES
    # Prefer the benchmark-EXCESS expectancy (alpha) as the headline; fall back
    # to raw expectancy before benchmark attribution exists.
    excess_exp = bench.get("excess_expectancy_pct")
    excess_lb = bench.get("excess_expectancy_lb95_pct")
    headline_exp = excess_exp if excess_exp is not None else perf.get("expectancy_pct")
    headline_lb = excess_lb if excess_lb is not None else perf.get("expectancy_lb95_pct")

    # Verdict: the edge is PROVEN only when the sample is large enough AND the
    # 95% lower bound on excess expectancy is above zero. Otherwise it is
    # PROMISING (positive point estimate, not yet significant) or NOT_PROVEN.
    ready = n >= target
    lb = headline_lb if headline_lb is not None else 0.0
    point = headline_exp if headline_exp is not None else 0.0
    if ready and lb > 0:
        verdict = "PROVEN"
    elif lb > 0:
        verdict = "SIGNIFICANT_BUT_UNDER_SAMPLE"
    elif point > 0:
        verdict = "PROMISING"
    else:
        verdict = "NO_EDGE_YET"

    return {
        "data": {
            "forward_trades": n,
            "target_trades": target,
            "progress_pct": round(min(100.0, n / target * 100.0), 1) if target else 0.0,
            "excess_expectancy_pct": headline_exp,
            "excess_expectancy_lb95_pct": headline_lb,
            "raw_expectancy_pct": perf.get("expectancy_pct"),
            "win_rate": perf.get("win_rate"),
            "win_rate_ci": perf.get("win_rate_ci"),
            "sharpe_per_trade": perf.get("sharpe_per_trade"),
            "max_drawdown_pct": perf.get("max_drawdown_pct"),
            "brier": brier,
            "benchmark_symbol": bench.get("symbol"),
            "attributed_trades": bench.get("attributed_trades"),
            "verdict": verdict,
            "ready": ready,
        }
    }


@router.get("/durability")
async def durability_report(backtest_win_rate: float = 0.50):
    """Durability check (D4): is the forward win rate inside the backtest CI?

    A divergence (no CI overlap) is the train→test collapse signature.
    """
    from app.services.forward_report import durability_check
    return {"data": await durability_check(backtest_win_rate=backtest_win_rate)}


@router.get("/gating/pending")
async def gating_pending():
    """A5: derived gating transitions awaiting human approval (veto mode)."""
    from app.services.gating_state import list_pending
    return {"data": await list_pending()}


@router.post("/gating/resolve")
async def gating_resolve(key: str, approve: bool):
    """A5: approve (apply) or reject (discard) a pending gating transition."""
    from app.services.gating_state import resolve_pending
    return {"data": await resolve_pending(key, approve)}


@router.post("/fit-weights")
async def fit_factor_weights(regime: Optional[str] = None):
    """Refit factor weights from resolved win/loss outcomes (logistic regression).

    Returns coefficients + normalised weight vector. Requires ≥200 resolved
    trades; below that returns `insufficient_data` and the engine keeps
    its hardcoded priors.
    """
    from app.services.recommendation_tuner import logistic_fit_weights
    result = await logistic_fit_weights(regime=regime)
    return {"data": result}


@router.post("/train-meta-label")
async def train_meta_label(n_splits: int = 5):
    """Fit + persist the meta-labeling secondary classifier (AFML Ch.3).

    Uses purged K-fold CV to estimate OOS accuracy. Predicts whether
    each primary recommendation will hit its target before its stop;
    output probability gates conviction at scoring time.
    """
    from app.services.ml_meta_label import train_meta_label_model
    return {"data": await train_meta_label_model(n_splits=n_splits)}


@router.post("/train-conviction-model")
async def train_conviction_model_endpoint():
    """Fit + persist the conviction model (2.4).

    Replaces the hand-tuned multiplicative conviction stack with one logistic
    model over the same factors — but only DEPLOYS it when it beats that stack
    on a chronological holdout. Otherwise the multiplicative stack keeps serving.
    """
    from app.services.conviction_model import train_conviction_model
    return {"data": await train_conviction_model()}


@router.get("/pipeline-bakeoff")
async def pipeline_bakeoff():
    """A-vs-B pipeline bake-off (2.1): survivor by OOS expectancy + Wilson-LB,
    plus whether a deletion is authorized (only after ≥300 forward trades)."""
    from app.services.pipeline_bakeoff import run_bakeoff, deletion_authorized
    return {"data": {
        "bakeoff": await run_bakeoff(),
        "deletion_gate": await deletion_authorized(),
    }}


@router.get("/forward/regime-verdict")
async def forward_regime_verdict():
    """4.4 — forward verdict split by market regime (trend_up/down/sideways)
    at each trade's entry, with Wilson bounds. An edge confined to one regime
    is visible here rather than hidden in a blended win rate."""
    from app.services.forward_report import regime_stratified_verdict
    return {"data": await regime_stratified_verdict()}


@router.get("/forward/selection-bias")
async def forward_selection_bias():
    """4.3 — selection-bias bound: taken-trade win rate vs a random ~5% shadow
    sample of REJECTED candidates (outcomes simulated). Measures what the funnel
    discards — the bias the meta-models, trained on taken trades only, can't see."""
    from app.services.shadow_sample import bias_report
    return {"data": await bias_report()}


@router.get("/summary")
async def performance_summary(window_days: int = 30):
    """Return rolling-window win rate, avg PnL, and signals evaluated.

    Defaults to a 30-day rolling window so the dashboard reflects recent
    behaviour rather than a lifetime aggregate that barely moves once the
    outcomes table grows past a few thousand rows. Pass ``window_days=0``
    for all-time stats.
    """
    try:
        win = None if window_days <= 0 else window_days
        summary = await get_performance_summary(window_days=win)
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

    # 3) OOS shipping-gate verdict from the latest walk-forward run.
    try:
        from pathlib import Path as _Path
        from app.services.oos_gate import latest_verdict
        gate = latest_verdict(_Path(__file__).resolve().parents[2] / "backtest_results")
        verdict = gate.get("verdict", "UNKNOWN")
        sev = {"PASS": "good", "REVIEW": "warn", "FAIL": "warn", "UNKNOWN": "info"}.get(verdict, "info")
        m = gate.get("metrics", {})
        insights.append({
            "kind": "oos_gate",
            "severity": sev,
            "title": (
                f"OOS shipping gate: {verdict}"
                + (f" — WR {m.get('win_rate')}%, avg P&L {m.get('avg_pnl_pct')}%, "
                   f"{m.get('total_trades')} trades" if m else "")
            ),
            "verdict": verdict,
            "shippable": gate.get("shippable", False),
            "reasons": gate.get("reasons", []),
            "metrics": m,
        })
    except Exception as e:
        logger.debug("OOS gate insight failed: %s", e)

    # 4) Static recommended mutes (always present as informational)
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


def _wilson_lower_bound(wins: int, total: int, z: float = 1.96) -> float:
    """Wilson score interval lower bound for a binomial proportion.

    We surface this on the cohort dashboard so a 60% WR over 5 outcomes
    isn't confused with the same WR over 500. Returns 0..100 as a percent.
    """
    if total <= 0:
        return 0.0
    p = wins / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * (((p * (1 - p) + z * z / (4 * total)) / total) ** 0.5)
    return round(max(0.0, (centre - margin) / denom) * 100.0, 2)


@router.get("/cohort")
async def performance_cohort(since: Optional[str] = None):
    """Per-signal-type WR / avg PnL / Wilson lower bound for outcomes since a date.

    `since` should be ISO YYYY-MM-DD. Defaults to 2026-05-26 (post conviction
    overhaul + mute rollout) so the dashboard reflects post-rule-change
    behaviour without lifetime contamination.
    """
    import aiosqlite
    from app.database import DB_PATH

    floor = (since or "2026-05-26")
    # Normalise: accept either YYYY-MM-DD or full ISO; we string-compare against
    # entry_time which is stored as ISO timestamp.
    try:
        # Validate by parsing — raises if user passes garbage.
        datetime.fromisoformat(floor.replace("Z", "+00:00")) if "T" in floor else datetime.strptime(floor, "%Y-%m-%d")
    except Exception:
        raise HTTPException(status_code=400, detail="since must be YYYY-MM-DD or ISO timestamp")

    rows: list[dict[str, Any]] = []
    totals = {"total": 0, "wins": 0, "losses": 0, "expired": 0, "open": 0, "pnl_sum": 0.0}
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT signal_type, direction,
                       COUNT(*) AS total,
                       SUM(CASE WHEN outcome='win'    THEN 1 ELSE 0 END) AS wins,
                       SUM(CASE WHEN outcome='loss'   THEN 1 ELSE 0 END) AS losses,
                       SUM(CASE WHEN outcome='expired' THEN 1 ELSE 0 END) AS expired,
                       SUM(CASE WHEN outcome='open' OR outcome IS NULL THEN 1 ELSE 0 END) AS open_cnt,
                       AVG(CASE WHEN pnl_pct IS NOT NULL THEN pnl_pct END) AS avg_pnl
                FROM signal_outcomes
                WHERE entry_time >= ?
                GROUP BY signal_type, direction
                ORDER BY total DESC
                """,
                (floor,),
            ) as cur:
                async for r in cur:
                    total = int(r["total"] or 0)
                    wins = int(r["wins"] or 0)
                    losses = int(r["losses"] or 0)
                    resolved = wins + losses
                    wr = round((wins / resolved) * 100.0, 2) if resolved else 0.0
                    rows.append({
                        "signal_type": r["signal_type"],
                        "direction": r["direction"],
                        "total": total,
                        "wins": wins,
                        "losses": losses,
                        "expired": int(r["expired"] or 0),
                        "open": int(r["open_cnt"] or 0),
                        "win_rate": wr,
                        "wilson_lb": _wilson_lower_bound(wins, resolved),
                        "avg_pnl_pct": round(float(r["avg_pnl"] or 0.0), 2),
                    })
                    totals["total"] += total
                    totals["wins"] += wins
                    totals["losses"] += losses
                    totals["expired"] += int(r["expired"] or 0)
                    totals["open"] += int(r["open_cnt"] or 0)
                    if r["avg_pnl"] is not None:
                        totals["pnl_sum"] += float(r["avg_pnl"]) * total

            # Recommendation-side cohort (tracked BUY/SELL only).
            reco = {"total": 0, "wins": 0, "losses": 0, "expired": 0, "open": 0}
            async with db.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN outcome='win'    THEN 1 ELSE 0 END) AS wins,
                       SUM(CASE WHEN outcome='loss'   THEN 1 ELSE 0 END) AS losses,
                       SUM(CASE WHEN outcome='expired' THEN 1 ELSE 0 END) AS expired,
                       SUM(CASE WHEN outcome IS NULL  THEN 1 ELSE 0 END) AS open_cnt
                FROM recommendation_outcomes
                WHERE created_at >= ?
                  AND action IN ('BUY','SELL')
                  AND COALESCE(tracked,1) = 1
                """,
                (floor,),
            ) as cur:
                r = await cur.fetchone()
                if r:
                    reco = {
                        "total": int(r["total"] or 0),
                        "wins": int(r["wins"] or 0),
                        "losses": int(r["losses"] or 0),
                        "expired": int(r["expired"] or 0),
                        "open": int(r["open_cnt"] or 0),
                    }

            # Engine considered (HOLD/AVOID, tracked=false) — visibility into
            # what the engine looked at but didn't act on.
            async with db.execute(
                """
                SELECT COUNT(*) AS held
                FROM recommendation_outcomes
                WHERE created_at >= ? AND COALESCE(tracked,1) = 0
                """,
                (floor,),
            ) as cur:
                r = await cur.fetchone()
                considered_holds = int(r["held"] or 0) if r else 0
    except Exception as e:
        logger.exception("cohort query failed: %s", e)
        raise HTTPException(status_code=500, detail="cohort query failed")

    resolved = totals["wins"] + totals["losses"]
    overall_wr = round((totals["wins"] / resolved) * 100.0, 2) if resolved else 0.0
    reco_resolved = reco["wins"] + reco["losses"]
    reco_wr = round((reco["wins"] / reco_resolved) * 100.0, 2) if reco_resolved else 0.0

    return {
        "since": floor,
        "signals": {
            "by_type": rows,
            "totals": {
                **totals,
                "win_rate": overall_wr,
                "wilson_lb": _wilson_lower_bound(totals["wins"], resolved),
            },
        },
        "recommendations": {
            **reco,
            "win_rate": reco_wr,
            "wilson_lb": _wilson_lower_bound(reco["wins"], reco_resolved),
            "considered_holds": considered_holds,
        },
    }


@router.post("/meta-judge/train")
async def trigger_meta_judge_train(n_stumps: int = 25, target_tpr: float = 0.70):
    """Re-train the deterministic meta-judge from signal_outcomes.

    Idempotent — re-running on the same DB produces the same model.
    Returns the train summary including OOS holdout metrics so the
    operator can decide whether to deploy the updated model.
    """
    try:
        from app.services.meta_judge_trainer import train_and_save
        result = await train_and_save(n_stumps=n_stumps, target_tpr=target_tpr)
        return {"data": result}
    except Exception as e:
        logger.exception("meta-judge train failed: %s", e)
        raise HTTPException(status_code=500, detail=f"train failed: {e}")


@router.get("/meta-judge/status")
async def meta_judge_status():
    """Inspect the currently-loaded meta-judge (if any)."""
    try:
        from app.services.meta_judge_trainer import _MODEL_PATH
        if not _MODEL_PATH.exists():
            return {"loaded": False, "reason": "no model file"}
        meta_path = _MODEL_PATH.with_suffix(".meta.json")
        if not meta_path.exists():
            return {"loaded": True, "meta": None}
        import json as _j
        return {"loaded": True, "meta": _j.loads(meta_path.read_text())}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/audited")
async def audited_metrics(since: Optional[str] = None):
    """Audited, public-grade performance metrics over tracked outcomes.

    Profit factor, expectancy, max drawdown, Sharpe, Brier score and a
    calibration curve — overall and split by horizon and by regime. The
    recommendation cohort carries `conviction`, so its calibration tells you
    whether the engine's confidence is honest. Signal outcomes are P&L-only
    (no stored probability), so Brier/calibration are null there.

    `since` (YYYY-MM-DD, default 2026-05-26) floors both cohorts to
    post-rule-change data so lifetime contamination doesn't skew the audit.
    """
    import aiosqlite
    from app.database import DB_PATH
    from app.services.performance_metrics import compute_metrics, group_metrics

    floor = since or "2026-05-26"
    try:
        datetime.strptime(floor, "%Y-%m-%d") if "T" not in floor else datetime.fromisoformat(floor.replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(status_code=400, detail="since must be YYYY-MM-DD or ISO timestamp")

    reco_trades: list[dict[str, Any]] = []
    signal_trades: list[dict[str, Any]] = []
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT conviction, pnl_pct, outcome, bars_held, timeframe_days,
                          horizon, regime
                   FROM recommendation_outcomes
                   WHERE pnl_pct IS NOT NULL AND created_at >= ?""",
                (floor,),
            ) as cur:
                async for r in cur:
                    conv = r["conviction"]
                    reco_trades.append({
                        "pnl_pct": r["pnl_pct"],
                        "outcome": r["outcome"],
                        # conviction (0-100) → predicted P(win); the calibration
                        # curve is the test of whether that mapping holds.
                        "predicted_prob": (float(conv) / 100.0) if conv is not None else None,
                        "hold_days": r["bars_held"] or r["timeframe_days"],
                        "horizon": r["horizon"],
                        "regime": r["regime"],
                    })
            async with db.execute(
                """SELECT pnl_pct, outcome, hold_days
                   FROM signal_outcomes
                   WHERE pnl_pct IS NOT NULL AND entry_time >= ?""",
                (floor,),
            ) as cur:
                async for r in cur:
                    signal_trades.append({
                        "pnl_pct": r["pnl_pct"],
                        "outcome": r["outcome"],
                        "hold_days": r["hold_days"],
                    })
    except Exception as e:
        logger.exception("audited metrics query failed: %s", e)
        raise HTTPException(status_code=500, detail="audited metrics query failed")

    return {
        "since": floor,
        "recommendations": {
            "overall": compute_metrics(reco_trades),
            "by_horizon": group_metrics(reco_trades, key=lambda t: t.get("horizon")),
            "by_regime": group_metrics(reco_trades, key=lambda t: t.get("regime")),
        },
        "signals": {
            "overall": compute_metrics(signal_trades),
        },
        "note": (
            "Win rate alone is a vanity metric. Expectancy and profit factor "
            "are what compound; the calibration curve shows whether conviction "
            "is honest. All numbers are post-cost on tracked outcomes only."
        ),
    }


@router.get("/oos-gate")
async def oos_shipping_gate(horizon: str = "5d"):
    """Out-of-sample shipping gate verdict from the latest walk-forward run.

    Returns PASS / REVIEW / FAIL / UNKNOWN. A config is only `shippable`
    when held-out, cost-aware numbers clear positive expectancy + a win-rate
    floor + Monte-Carlo p5 (ADR-9 fragility) + a minimum sample. This is the
    gate the user-facing 'this is a money-maker' claim must pass first.
    """
    if horizon not in {"1d", "3d", "5d", "10d"}:
        raise HTTPException(status_code=400, detail="horizon must be 1d, 3d, 5d, or 10d")
    from pathlib import Path as _Path
    from app.services.oos_gate import latest_verdict
    results_dir = _Path(__file__).resolve().parents[2] / "backtest_results"
    return {"data": latest_verdict(results_dir, horizon=horizon)}


@router.get("/automation-status")
async def automation_status():
    """Is the autonomous engine actually running? Evidence, not assumptions.

    Returns market-open state, the last-run heartbeat for each loop
    (scan / auto-paper / daily+weekly backtest), the next scheduled backtest
    times, whether auto-paper is enabled, and the current open-position count.
    """
    import aiosqlite
    from app.database import DB_PATH
    from app.services.runtime_status import get_status
    from app.services.orchestrator import (
        is_market_open, orchestrator, _get_settings,
        _next_daily_at, _next_weekly_backtest_dt,
    )

    heartbeats = await get_status()
    db_settings = await _get_settings()
    auto_paper_enabled = str(
        db_settings.get("auto_paper_trade_enabled", "true")
    ).lower() in {"1", "true", "yes", "on"}
    daily_backtest_enabled = str(
        db_settings.get("daily_backtest_enabled", "true")
    ).lower() in {"1", "true", "yes", "on"}

    open_positions = 0
    last_backtest_at = None
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE status='open'"
            ) as cur:
                row = await cur.fetchone()
                open_positions = int(row[0]) if row else 0
            async with db.execute(
                "SELECT MAX(run_at) FROM backtest_runs"
            ) as cur:
                row = await cur.fetchone()
                last_backtest_at = row[0] if row else None
    except Exception as e:
        logger.debug("automation-status db read failed: %s", e)

    return {
        "data": {
            "orchestrator_running": orchestrator.is_running(),
            "market_open": is_market_open(),
            "auto_paper_enabled": auto_paper_enabled,
            "daily_backtest_enabled": daily_backtest_enabled,
            "open_positions": open_positions,
            "heartbeats": heartbeats,
            "last_backtest_at": last_backtest_at,
            "next_daily_backtest_utc": _next_daily_at(11, 0).isoformat(),
            "next_weekly_backtest_utc": _next_weekly_backtest_dt().isoformat(),
        }
    }


class MetaJudgeExplainRequest(BaseModel):
    features: dict[str, Any] = Field(
        description="Trade feature dict (signal_type, direction, regime, factor scores, etc.)"
    )
    top_k: Optional[int] = Field(default=None, ge=1, le=50)


@router.post("/meta-judge/explain")
async def meta_judge_explain(body: MetaJudgeExplainRequest):
    """Exact per-feature attribution for the meta-judge's verdict on a trade.

    Returns the additive contribution of each feature to the decision margin
    (SHAP-grade for this stump ensemble: Σ contributions == margin), plus the
    resulting P(win) and keep/drop verdict.
    """
    from app.services.meta_judge_trainer import _MODEL_PATH
    from app.services.meta_judge import MetaJudge
    if not _MODEL_PATH.exists():
        raise HTTPException(status_code=404, detail="no trained meta-judge model — train it first")
    try:
        model = MetaJudge.load(_MODEL_PATH)
        return {"data": model.explain(body.features, top_k=body.top_k)}
    except Exception as e:
        logger.exception("meta-judge explain failed: %s", e)
        raise HTTPException(status_code=500, detail=f"explain failed: {e}")


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

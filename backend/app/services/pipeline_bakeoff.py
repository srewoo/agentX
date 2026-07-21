from __future__ import annotations
"""2.1 — Pipeline A vs B bake-off: decide the survivor by holdout performance.

Today two scoring pipelines can disagree on the same stock:
  * **A** — ``signal_engine`` → ``meta_judge`` (AdaBoost on ``signal_outcomes``);
  * **B** — ``recommendation`` → ``ml_meta_label`` (GBM on ``recommendation_outcomes``),
    the pipeline the forward paper-trader actually trades.

target9 §2.1 asks to unify to one path and "decide the survivor by holdout
performance, delete the loser." This module builds the missing seam: a
head-to-head comparator on a common metric (per-trade expectancy net of costs +
Wilson-LB win rate) over the same out-of-sample window.

Two hard rails, both encoded here rather than left to judgement:

  1. **Comparability caveat.** A's ``signal_outcomes`` are price-move shadows;
    B's ``recommendation_outcomes`` are SL/target simulations. Their pnl is NOT
    yet apples-to-apples, so the verdict is advisory until both are run through
    the same harness (``portfolio_backtester``). The caveat rides in every result.
  2. **No deletion without forward evidence.** ``deletion_authorized`` stays
    False until the surviving pipeline has cleared the pre-registered forward
    bar (≥300 closed paper trades). Deleting a pipeline on backtest alone is the
    evidence-free, irreversible move target9 forbids — the gate makes it
    impossible in code, not just in policy.

The stats are pure so they unit-test cleanly; the DB loaders are thin.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

import aiosqlite

from app.database import DB_PATH
from app.services.forward_report import wilson_interval

logger = logging.getLogger(__name__)

_MIN_SAMPLE = 50           # per-pipeline OOS trades before a verdict means anything
_COMPARABILITY_CAVEAT = (
    "Pipeline A outcomes are price-move shadows (signal_outcomes); B are "
    "SL/target simulations (recommendation_outcomes). PnL is not yet directly "
    "comparable — run both through portfolio_backtester for a fair verdict. "
    "This comparison is ADVISORY."
)


@dataclass
class PipelineStats:
    name: str
    n: int
    wins: int
    win_rate: float
    win_rate_lb: float          # Wilson lower bound (small-sample honest)
    expectancy_pct: float       # mean net pnl per trade


@dataclass
class BakeoffVerdict:
    a: PipelineStats
    b: PipelineStats
    survivor: Optional[str]     # "A" | "B" | None (inconclusive)
    reason: str
    caveat: str = _COMPARABILITY_CAVEAT


def _stats(name: str, pnls: list[float]) -> PipelineStats:
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    lo, _ = wilson_interval(wins, n)
    return PipelineStats(
        name=name, n=n, wins=wins,
        win_rate=round(wins / n, 4) if n else 0.0,
        win_rate_lb=round(lo, 4),
        expectancy_pct=round(sum(pnls) / n, 4) if n else 0.0,
    )


def compare(a_pnls: list[float], b_pnls: list[float]) -> BakeoffVerdict:
    """Decide a survivor from two OOS pnl-per-trade samples.

    A pipeline wins only if BOTH samples are adequate AND it strictly beats the
    other on expectancy while its Wilson-LB win rate is not worse. Ties and
    thin samples return None (inconclusive) — the honest default that refuses to
    pick a winner on noise.
    """
    a, b = _stats("A", a_pnls), _stats("B", b_pnls)
    if a.n < _MIN_SAMPLE or b.n < _MIN_SAMPLE:
        return BakeoffVerdict(a, b, None,
                              f"inconclusive — need ≥{_MIN_SAMPLE} OOS trades each "
                              f"(A={a.n}, B={b.n})")
    if abs(a.expectancy_pct - b.expectancy_pct) < 1e-6:
        return BakeoffVerdict(a, b, None, "inconclusive — expectancies tied")
    winner, loser = (a, b) if a.expectancy_pct > b.expectancy_pct else (b, a)
    if winner.win_rate_lb < loser.win_rate_lb:
        return BakeoffVerdict(
            a, b, None,
            f"inconclusive — {winner.name} leads on expectancy but trails on "
            f"Wilson-LB win rate ({winner.win_rate_lb} < {loser.win_rate_lb})")
    return BakeoffVerdict(
        a, b, winner.name,
        f"{winner.name} wins: expectancy {winner.expectancy_pct} vs "
        f"{loser.expectancy_pct}, Wilson-LB WR {winner.win_rate_lb} ≥ {loser.win_rate_lb}")


async def _load_pnls(query: str, *, db_path: str) -> list[float]:
    out: list[float] = []
    try:
        async with aiosqlite.connect(db_path) as db:
            async with db.execute(query) as cur:
                out = [float(r[0]) for r in await cur.fetchall() if r[0] is not None]
    except Exception as e:
        logger.debug("bakeoff load failed: %s", e)
    return out


async def run_bakeoff(*, db_path: Optional[str] = None) -> dict:
    """Load both pipelines' resolved OOS outcomes and compare, honoring the
    pinned holdout (never scores the reserved window)."""
    from app.services import holdout as _holdout
    path = db_path or DB_PATH
    boundary = await _holdout.resolve_boundary(db_path=path)
    cutoff = f" AND created_at <= '{boundary.isoformat()}'" if boundary else ""

    # A: signal_outcomes (price-move). B: recommendation_outcomes (SL/target).
    a_pnls = await _load_pnls(
        f"SELECT pnl_pct FROM signal_outcomes WHERE pnl_pct IS NOT NULL{cutoff}", db_path=path)
    b_pnls = await _load_pnls(
        "SELECT pnl_pct FROM recommendation_outcomes WHERE outcome IN ('win','loss') "
        f"AND pnl_pct IS NOT NULL{cutoff}", db_path=path)

    verdict = compare(a_pnls, b_pnls)
    return {
        "pipeline_a": vars(verdict.a),
        "pipeline_b": vars(verdict.b),
        "survivor": verdict.survivor,
        "reason": verdict.reason,
        "caveat": verdict.caveat,
        "holdout_boundary": boundary.isoformat() if boundary else None,
    }


async def deletion_authorized(*, db_path: Optional[str] = None) -> dict:
    """May the losing pipeline be deleted yet? Only after the survivor has
    cleared the pre-registered FORWARD bar (≥300 closed paper trades) — backtest
    evidence alone never authorizes an irreversible merge (target9 rule)."""
    from app.services.forward_report import forward_performance, DEFAULT_TARGET_TRADES
    fwd = await forward_performance(db_path=db_path or DB_PATH)
    n = int(fwd.get("trades") or 0)
    ready = bool(fwd.get("readiness", {}).get("ready"))
    return {
        "authorized": ready,
        "forward_trades": n,
        "required": DEFAULT_TARGET_TRADES,
        "reason": (
            f"authorized — {n} forward trades ≥ {DEFAULT_TARGET_TRADES}"
            if ready else
            f"BLOCKED — {n}/{DEFAULT_TARGET_TRADES} forward trades; no pipeline "
            "deletion on backtest evidence alone"
        ),
    }

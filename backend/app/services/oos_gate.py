from __future__ import annotations
"""Out-of-sample shipping gate.

`scripts/oos_ceiling.py` already proved that an *in-sample* "ceiling" is a
fiction — combos that look profitable on the data they were picked on
collapse 20-30pp on held-out folds. This module turns that lesson into an
enforceable gate: **before any signal config is trusted as shippable, its
walk-forward out-of-sample numbers must clear hard, cost-aware bars.**

A config is shippable only when, on held-out data net of realistic costs:

  • positive expectancy        — universe avg P&L per trade ≥ 0
  • better than a coin flip     — universe win rate ≥ ``min_win_rate``
  • survives randomisation      — Monte-Carlo 5th-pct WR ≥ ``min_mc_p5_wr``
                                  (ADR-9: p5 < 45% ⇒ regime-fragile)
  • enough evidence             — ≥ ``min_trades`` OOS trades

Metrics are aggregated trade-weighted across the per-symbol walk-forward
output (the list of dicts each carrying an ``oos_summary``), so one
high-volume name can't be hidden behind a quiet one.
"""
import glob
import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Default bars — conservative, aligned with the backtest_results analysis.
DEFAULT_MIN_TRADES = 100
DEFAULT_MIN_WIN_RATE = 45.0
DEFAULT_MIN_AVG_PNL = 0.0
DEFAULT_MIN_MC_P5_WR = 45.0


def _w_avg(pairs: list[tuple[float, float]]) -> Optional[float]:
    """Trade-weighted average of (value, weight) pairs; None if no weight."""
    num = sum(v * w for v, w in pairs)
    den = sum(w for _, w in pairs)
    return (num / den) if den > 0 else None


def aggregate_oos(results: list[dict[str, Any]], horizon: str = "5d") -> dict[str, Any]:
    """Trade-weighted universe metrics for a horizon across per-symbol runs.

    ``results`` is the walk-forward output: a list of per-symbol dicts each
    with an ``oos_summary`` carrying ``win_rate_<h>`` / ``avg_pnl_<h>`` /
    ``mc_wr_p5_<h>`` / ``win_rate_lb95_<h>`` and a ``trades`` count.
    """
    wr_pairs: list[tuple[float, float]] = []
    pnl_pairs: list[tuple[float, float]] = []
    mc_pairs: list[tuple[float, float]] = []
    lb_pairs: list[tuple[float, float]] = []
    total_trades = 0
    symbols = 0

    for r in results:
        s = r.get("oos_summary") if isinstance(r, dict) else None
        if not isinstance(s, dict):
            continue
        t = float(s.get("trades") or 0)
        if t <= 0:
            continue
        symbols += 1
        total_trades += int(t)
        wr = s.get(f"win_rate_{horizon}")
        pnl = s.get(f"avg_pnl_{horizon}")
        mc = s.get(f"mc_wr_p5_{horizon}")
        lb = s.get(f"win_rate_lb95_{horizon}")
        if wr is not None:
            wr_pairs.append((float(wr), t))
        if pnl is not None:
            pnl_pairs.append((float(pnl), t))
        if mc is not None:
            mc_pairs.append((float(mc), t))
        if lb is not None:
            lb_pairs.append((float(lb), t))

    def _round(x: Optional[float], n: int = 4) -> Optional[float]:
        return round(x, n) if x is not None else None

    return {
        "horizon": horizon,
        "symbols": symbols,
        "total_trades": total_trades,
        "win_rate": _round(_w_avg(wr_pairs), 2),
        "avg_pnl_pct": _round(_w_avg(pnl_pairs)),
        "mc_wr_p5": _round(_w_avg(mc_pairs), 2),
        "win_rate_lb95": _round(_w_avg(lb_pairs), 2),
    }


def evaluate_oos_gate(
    results: list[dict[str, Any]],
    *,
    horizon: str = "5d",
    min_trades: int = DEFAULT_MIN_TRADES,
    min_win_rate: float = DEFAULT_MIN_WIN_RATE,
    min_avg_pnl: float = DEFAULT_MIN_AVG_PNL,
    min_mc_p5_wr: float = DEFAULT_MIN_MC_P5_WR,
) -> dict[str, Any]:
    """Evaluate the OOS shipping gate.

    Returns ``{shippable, verdict, metrics, reasons, thresholds}`` where
    verdict is PASS / REVIEW / FAIL:
      • FAIL   — negative expectancy (the disqualifier) or no data.
      • REVIEW — positive expectancy but a robustness/sample bar missed.
      • PASS   — every bar cleared.
    """
    metrics = aggregate_oos(results, horizon=horizon)
    thresholds = {
        "min_trades": min_trades,
        "min_win_rate": min_win_rate,
        "min_avg_pnl": min_avg_pnl,
        "min_mc_p5_wr": min_mc_p5_wr,
    }
    reasons: list[str] = []

    if metrics["total_trades"] == 0 or metrics["avg_pnl_pct"] is None:
        return {
            "shippable": False, "verdict": "FAIL",
            "metrics": metrics, "reasons": ["no out-of-sample data"],
            "thresholds": thresholds,
        }

    avg_pnl = metrics["avg_pnl_pct"]
    wr = metrics["win_rate"] or 0.0
    mc_p5 = metrics["mc_wr_p5"]
    n = metrics["total_trades"]

    # Hard disqualifier: negative expectancy net of costs.
    neg_expectancy = avg_pnl < min_avg_pnl
    if neg_expectancy:
        reasons.append(
            f"negative expectancy: avg P&L {avg_pnl:+.4f}% < {min_avg_pnl}% (net of costs)"
        )

    # Soft (REVIEW-grade) bars.
    if wr < min_win_rate:
        reasons.append(f"win rate {wr:.1f}% < {min_win_rate}%")
    if mc_p5 is not None and mc_p5 < min_mc_p5_wr:
        reasons.append(
            f"Monte-Carlo p5 WR {mc_p5:.1f}% < {min_mc_p5_wr}% (regime-fragile per ADR-9)"
        )
    if n < min_trades:
        reasons.append(f"only {n} OOS trades < {min_trades} (insufficient evidence)")

    if neg_expectancy:
        verdict = "FAIL"
    elif reasons:  # +EV but a robustness/sample bar missed
        verdict = "REVIEW"
    else:
        verdict = "PASS"

    return {
        "shippable": verdict == "PASS",
        "verdict": verdict,
        "metrics": metrics,
        "reasons": reasons or ["all out-of-sample bars cleared"],
        "thresholds": thresholds,
    }


def load_latest_walk_forward(
    results_dir: str | Path = "backtest_results",
) -> tuple[Optional[list[dict[str, Any]]], Optional[str]]:
    """Load the newest ``walk_fwd_*.json``. Returns ``(results, path)`` or
    ``(None, None)`` when no walk-forward output exists yet."""
    try:
        base = Path(results_dir)
        files = sorted(base.glob("walk_fwd_*.json"))
        if not files:
            return None, None
        latest = files[-1]
        data = json.loads(latest.read_text())
        if isinstance(data, dict):  # single-symbol shape — normalise to a list
            data = [data]
        return data, str(latest)
    except Exception as e:
        logger.warning("Failed to load latest walk-forward: %s", e)
        return None, None


def latest_verdict(
    results_dir: str | Path = "backtest_results", *, horizon: str = "5d", **kwargs: Any
) -> dict[str, Any]:
    """Convenience: load the latest walk-forward and evaluate the gate.

    Returns ``verdict='UNKNOWN'`` when no walk-forward output exists, so the
    caller can distinguish "never measured" from "measured and failed".
    """
    results, path = load_latest_walk_forward(results_dir)
    if results is None:
        return {
            "shippable": False, "verdict": "UNKNOWN",
            "metrics": {}, "reasons": ["no walk-forward output found — run the backtester"],
            "thresholds": {}, "source": None,
        }
    out = evaluate_oos_gate(results, horizon=horizon, **kwargs)
    out["source"] = path
    return out

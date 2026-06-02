from __future__ import annotations
"""Audited, public-grade performance metrics.

Win rate alone is a vanity metric — a 75% hit rate with fat losers loses
money, a 45% hit rate with 3:1 winners compounds. This module computes the
metrics that actually describe an edge, net of nothing it can't see:

  • hit_rate, avg_win, avg_loss, payoff_ratio
  • profit_factor   = gross_profit / gross_loss
  • expectancy      = mean P&L per trade (the number that compounds)
  • max_drawdown    = worst peak-to-trough on the equal-weight equity curve
  • sharpe          = per-trade and (when holding period is known) annualised
  • brier_score     = calibration error of the predicted win probability
  • calibration     = reliability curve (predicted prob vs realised win rate)

Everything here is a pure function over a list of resolved trades, so it is
trivially testable and reusable by the API layer, the backtester, and the
weekly digest. A "resolved" trade is one with a non-null ``pnl_pct``.

Brier/calibration require a predicted probability per trade. agentX stores
``conviction`` (0-100) on every tracked recommendation; ``conviction / 100``
is the natural predicted probability, and the reliability curve is exactly
the test of whether that conviction is honest.
"""
import math
from typing import Any, Callable, Iterable, Optional


def _is_win(t: dict[str, Any]) -> Optional[bool]:
    """Win/loss for a trade. Prefers an explicit ``outcome``, else sign of P&L."""
    outcome = t.get("outcome")
    if outcome in ("win", "loss"):
        return outcome == "win"
    pnl = t.get("pnl_pct")
    if pnl is None:
        return None
    return float(pnl) > 0.0


def max_drawdown_pp(pnls: list[float]) -> float:
    """Worst peak-to-trough drawdown (percentage points) on the equal-weight
    cumulative-P&L curve, in trade order. Returned as a positive magnitude."""
    peak = 0.0
    cum = 0.0
    worst = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return round(abs(worst), 4)


def _calibration_curve(
    pairs: list[tuple[float, int]], n_bins: int = 10
) -> list[dict[str, Any]]:
    """Reliability curve. ``pairs`` = [(predicted_prob, win01), ...]."""
    bins: list[dict[str, Any]] = []
    for b in range(n_bins):
        lo = b / n_bins
        hi = (b + 1) / n_bins
        # Last bin is closed on the right so prob == 1.0 lands somewhere.
        members = [
            (p, y) for (p, y) in pairs
            if (lo <= p < hi) or (b == n_bins - 1 and p == 1.0)
        ]
        if not members:
            continue
        n = len(members)
        mean_pred = sum(p for p, _ in members) / n
        observed = sum(y for _, y in members) / n
        bins.append({
            "bin": f"{lo:.1f}-{hi:.1f}",
            "count": n,
            "mean_predicted": round(mean_pred, 4),
            "observed_win_rate": round(observed, 4),
            "gap": round(observed - mean_pred, 4),
        })
    return bins


def compute_metrics(
    trades: list[dict[str, Any]],
    *,
    annualise: bool = True,
    trading_days: int = 252,
) -> dict[str, Any]:
    """Compute the audited metric bundle over a list of trade dicts.

    Each trade may carry: ``pnl_pct`` (required to be resolved),
    ``outcome`` ('win'|'loss'|...), ``predicted_prob`` (0..1) and
    ``hold_days`` (for Sharpe annualisation). Trades in the list are assumed
    to be in chronological order for the drawdown curve.
    """
    resolved = [t for t in trades if t.get("pnl_pct") is not None]
    pnls = [float(t["pnl_pct"]) for t in resolved]
    n_total = len(trades)
    n_resolved = len(resolved)

    base: dict[str, Any] = {
        "n_total": n_total,
        "n_resolved": n_resolved,
        "wins": 0,
        "losses": 0,
        "hit_rate": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "payoff_ratio": None,
        "profit_factor": None,
        "expectancy": 0.0,
        "max_drawdown_pp": 0.0,
        "sharpe_per_trade": 0.0,
        "sharpe_annualised": None,
        "brier_score": None,
        "calibration": [],
    }
    if n_resolved == 0:
        return base

    wins_pnl = [p for p in pnls if p > 0]
    loss_pnl = [p for p in pnls if p < 0]
    wins = len(wins_pnl)
    losses = len(loss_pnl)
    decided = wins + losses

    gross_profit = sum(wins_pnl)
    gross_loss = abs(sum(loss_pnl))
    avg_win = (gross_profit / wins) if wins else 0.0
    avg_loss = (sum(loss_pnl) / losses) if losses else 0.0  # negative

    expectancy = sum(pnls) / n_resolved
    mean = expectancy
    var = sum((p - mean) ** 2 for p in pnls) / n_resolved
    sd = math.sqrt(var)
    sharpe_pt = (mean / sd) if sd > 0 else 0.0

    sharpe_ann: Optional[float] = None
    if annualise:
        holds = [float(t["hold_days"]) for t in resolved
                 if t.get("hold_days") not in (None, 0)]
        if holds and sharpe_pt:
            mean_hold = sum(holds) / len(holds)
            trades_per_year = trading_days / max(1.0, mean_hold)
            sharpe_ann = round(sharpe_pt * math.sqrt(trades_per_year), 4)

    base.update({
        "wins": wins,
        "losses": losses,
        "hit_rate": round((wins / decided) * 100.0, 2) if decided else 0.0,
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "payoff_ratio": round(avg_win / abs(avg_loss), 4) if avg_loss < 0 else None,
        # PF is undefined (no downside) when there are zero losing trades.
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else None,
        "expectancy": round(expectancy, 4),
        "max_drawdown_pp": max_drawdown_pp(pnls),
        "sharpe_per_trade": round(sharpe_pt, 4),
        "sharpe_annualised": sharpe_ann,
    })

    # Brier + calibration (only for trades with a predicted probability).
    cal_pairs: list[tuple[float, int]] = []
    sq_err = 0.0
    n_prob = 0
    for t in resolved:
        prob = t.get("predicted_prob")
        win = _is_win(t)
        if prob is None or win is None:
            continue
        p = max(0.0, min(1.0, float(prob)))
        y = 1 if win else 0
        sq_err += (p - y) ** 2
        cal_pairs.append((p, y))
        n_prob += 1
    if n_prob > 0:
        base["brier_score"] = round(sq_err / n_prob, 4)
        base["calibration"] = _calibration_curve(cal_pairs)

    return base


def group_metrics(
    trades: list[dict[str, Any]],
    key: Callable[[dict[str, Any]], Optional[str]],
    **kwargs: Any,
) -> dict[str, dict[str, Any]]:
    """Compute :func:`compute_metrics` per group (e.g. by horizon or regime).

    ``key`` returns the group label for a trade, or ``None`` to skip it.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        label = key(t)
        if label is None:
            continue
        groups.setdefault(str(label), []).append(t)
    return {label: compute_metrics(rows, **kwargs) for label, rows in groups.items()}

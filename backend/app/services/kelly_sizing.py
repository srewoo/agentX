from __future__ import annotations
"""Edge-aware Kelly-fractional position sizing.

The walk-forward evidence (see backtest_results/) is unambiguous on one
point: the signal engine already *generates* the winning trades — the entire
edge lives in **which trades to skip and how much to bet**, not in
generating better signals. An oracle that merely sized the best 30% of
trades to their edge and the rest to zero swings 5y P&L by +6,200pp.

This module is the deterministic approximation of that oracle. It sizes a
trade to its measured edge using the Kelly criterion, with three guards that
make estimation error survivable:

  1. **Fractional Kelly** — we bet a *fraction* (default ¼) of full Kelly.
     Full Kelly maximises log-growth but has brutal variance and is
     ruinous when `p`/`b` are mis-estimated. Quarter-Kelly keeps ~ the same
     growth at a quarter of the drawdown.
  2. **Hard position cap** — never more than `max_position_pct` of capital
     in one name (the HDFCBANK −67% lesson: no single position should be
     able to wreck the book).
  3. **Per-trade risk cap** — shares are also bounded so the stop-loss
     distance × shares never risks more than `max_risk_pct` of capital.

Crucially: when the Kelly fraction is **≤ 0** (no positive expectancy at the
given win-probability and reward:risk), the size is **0** and the caller
skips the trade. Negative-edge setups get no capital. That is the filter.

The payoff ratio `b` is taken from the *real* trade geometry
(reward = target−entry, risk = entry−stop) — not an assumed distribution —
so the only estimated input is the win probability `p`.

Conservatism on `p` is now **enforced, not merely requested**:
  * a hard ``MAX_WIN_PROB`` ceiling (0.85) caps the edge any single input may
    imply, so an over-optimistic caller cannot size for ruin; and
  * when the caller supplies ``win_prob_n`` (the sample size `p` was measured
    on), the size is computed from the **Wilson 95% lower bound** of that
    sample, not the raw point estimate — small lucky samples are sized down
    automatically.
Callers that already pass a calibrated probability shrunk toward 0.5 (e.g. the
conviction→prob map in auto_paper_trader, or a meta-label output) keep working
unchanged; the guards only ever make sizing *more* conservative.
"""
import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

# Defaults — deliberately conservative. A settings UI can override later.
DEFAULT_KELLY_FRACTION = 0.25   # ¼-Kelly
DEFAULT_MAX_POSITION_PCT = 5.0  # hard per-position cap (% of capital)
DEFAULT_MAX_RISK_PCT = 1.0      # max capital risked to the stop (% of capital)

# 1.1 — exposure-preserving per-trade cap. Widening the funnel means a larger
# open book; if each position kept its old 5% cap, total gross exposure would
# balloon with the position count. Instead the per-position cap SHRINKS with the
# book size so peak gross exposure stays ≈ TARGET_GROSS_EXPOSURE_PCT regardless
# of how many names are open — "lower per-trade notional so total exposure is
# unchanged". Never exceeds the hard DEFAULT_MAX_POSITION_PCT.
TARGET_GROSS_EXPOSURE_PCT = 60.0


def per_position_cap_pct(
    max_open_positions: int,
    *,
    target_gross_pct: float = TARGET_GROSS_EXPOSURE_PCT,
    hard_cap_pct: float = DEFAULT_MAX_POSITION_PCT,
) -> float:
    """Per-position cap (% of capital) that keeps peak gross exposure bounded.

    = min(hard_cap, target_gross / max_open). With the historical book of 12 this
    returns 5% (unchanged); at a widened book of 30 it returns 2%, so 30 full
    positions still sum to ~60% gross rather than 150%.
    """
    if max_open_positions <= 0:
        return hard_cap_pct
    return min(hard_cap_pct, target_gross_pct / max_open_positions)

# Structural ceiling on the win probability Kelly is allowed to act on.
# The walk-forward evidence shows even the best setups realise ~53-62% WR;
# no honest per-trade estimate should imply more edge than ~0.85. This is a
# *last line of defence* so that a caller passing an over-optimistic p (e.g.
# a raw empirical 0.95 on a handful of trades) can never size for ruin. It
# does not replace per-signal Wilson shrinkage — it backstops it.
MAX_WIN_PROB = 0.85


def wilson_lower_bound(wins: int, n: int, z: float = 1.96) -> float:
    """Wilson 95% lower bound on a win rate.

    The conservative win probability to plan around: it shrinks the point
    estimate toward 0.5 in proportion to how little data supports it, so a
    55% rate on 20 trades is treated very differently from 55% on 2,000.
    Returns ``0.0`` for an empty sample (no evidence ⇒ no edge).
    """
    if n <= 0:
        return 0.0
    phat = max(0.0, min(1.0, wins / n))
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = phat + z2 / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z2 / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def payoff_ratio(entry: float, stop: float, target: float, direction: str) -> float | None:
    """Reward:risk multiple ``b`` from real trade levels.

    ``b = reward / risk`` where reward and risk are the per-share distances to
    target and stop in the direction of the trade. Returns ``None`` when the
    geometry is invalid (non-positive risk or reward), which the caller must
    treat as "do not size".
    """
    if entry <= 0:
        return None
    if direction == "bullish":
        risk = entry - stop
        reward = target - entry
    else:  # bearish
        risk = stop - entry
        reward = entry - target
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


def kelly_fraction(win_prob: float, b: float) -> float:
    """Full-Kelly fraction for a binary bet with general payoff ``b``.

        f* = p − (1 − p) / b

    Clamped at 0 on the downside: a negative ``f*`` means the bet is
    −EV and the correct size is nothing. Not clamped on the upside here —
    the fractional multiplier and caps in :func:`kelly_position_size`
    handle that.
    """
    if b <= 0:
        return 0.0
    # Clamp to [0, MAX_WIN_PROB]: no input is allowed to imply more edge than
    # the historical evidence supports, so f* cannot blow up on a bad p.
    p = max(0.0, min(MAX_WIN_PROB, win_prob))
    f = p - (1.0 - p) / b
    return max(0.0, f)


def kelly_position_size(
    capital: float,
    entry: float,
    stop: float,
    target: float,
    win_prob: float,
    *,
    win_prob_n: int | None = None,
    direction: str = "bullish",
    kelly_fraction_mult: float = DEFAULT_KELLY_FRACTION,
    max_position_pct: float = DEFAULT_MAX_POSITION_PCT,
    max_risk_pct: float = DEFAULT_MAX_RISK_PCT,
) -> dict[str, Any]:
    """Size a position to its measured edge via fractional Kelly.

    Returns a dict::

        {
            "shares": int,              # 0 ⇒ skip the trade
            "skip": bool,               # True when shares == 0
            "reason": str,              # why (esp. when skipped)
            "position_value": float,    # shares × entry
            "capital_pct": float,       # position_value as % of capital
            "risk_amount": float,       # shares × stop-distance (₹ at risk)
            "kelly_f_full": float,      # full-Kelly fraction
            "kelly_f_used": float,      # after the fractional multiplier
            "payoff_ratio": float,      # b = reward / risk
            "binding_constraint": str,  # kelly | position_cap | risk_cap
        }

    A skipped trade (``shares == 0``) is the deterministic equivalent of the
    oracle dropping a negative-edge candidate.
    """
    base = {
        "shares": 0, "skip": True, "reason": "", "position_value": 0.0,
        "capital_pct": 0.0, "risk_amount": 0.0, "kelly_f_full": 0.0,
        "kelly_f_used": 0.0, "payoff_ratio": 0.0, "binding_constraint": "kelly",
        "win_prob_raw": round(float(win_prob), 4), "win_prob_used": round(float(win_prob), 4),
    }
    if capital <= 0 or entry <= 0:
        base["reason"] = "invalid capital/entry"
        return base

    b = payoff_ratio(entry, stop, target, direction)
    if b is None:
        base["reason"] = "invalid trade geometry (non-positive risk or reward)"
        return base
    base["payoff_ratio"] = round(b, 4)

    # Conservative-p guard. When the caller supplies the sample size the
    # win_prob was measured on (``win_prob_n``), we never size off the raw
    # point estimate — we size off its Wilson 95% lower bound. This makes it
    # *structurally impossible* to oversize on a lucky small sample: e.g. a
    # 55% rate on 80 trades is sized as ~44%, which on poor odds drops the
    # trade entirely. Callers that already pass a calibrated/shrunk p (no n)
    # are unaffected.
    p_used = win_prob
    if win_prob_n is not None and win_prob_n > 0:
        p_used = min(win_prob, wilson_lower_bound(round(win_prob * win_prob_n), win_prob_n))
    base["win_prob_raw"] = round(float(win_prob), 4)
    base["win_prob_used"] = round(float(p_used), 4)

    f_full = kelly_fraction(p_used, b)
    base["kelly_f_full"] = round(f_full, 4)
    if f_full <= 0:
        base["reason"] = (
            f"non-positive Kelly edge (p={p_used:.2f}, b={b:.2f}) — skip"
        )
        return base

    f_used = f_full * max(0.0, kelly_fraction_mult)
    base["kelly_f_used"] = round(f_used, 4)

    # Three candidate share counts; the most conservative (smallest) wins.
    stop_distance = abs(entry - stop)
    kelly_shares = int((f_used * capital) / entry)
    cap_shares = int((capital * max_position_pct / 100.0) / entry)
    risk_shares = (
        int((capital * max_risk_pct / 100.0) / stop_distance)
        if stop_distance > 0 else kelly_shares
    )

    shares = min(kelly_shares, cap_shares, risk_shares)
    if shares <= 0:
        base["reason"] = "sized below 1 share after caps"
        return base

    binding = "kelly"
    if shares == cap_shares and cap_shares < kelly_shares:
        binding = "position_cap"
    elif shares == risk_shares and risk_shares < kelly_shares:
        binding = "risk_cap"

    position_value = round(shares * entry, 2)
    return {
        "shares": shares,
        "skip": False,
        "reason": "ok",
        "position_value": position_value,
        "capital_pct": round(position_value / capital * 100.0, 3),
        "risk_amount": round(shares * stop_distance, 2),
        "kelly_f_full": round(f_full, 4),
        "kelly_f_used": round(f_used, 4),
        "payoff_ratio": round(b, 4),
        "binding_constraint": binding,
        "win_prob_raw": round(float(win_prob), 4),
        "win_prob_used": round(float(p_used), 4),
    }

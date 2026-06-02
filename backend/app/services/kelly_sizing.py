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
so the only estimated input is the win probability `p`, which callers should
source conservatively (measured per-signal win-rate Wilson lower bound, or a
calibrated probability shrunk toward 0.5).
"""
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Defaults — deliberately conservative. A settings UI can override later.
DEFAULT_KELLY_FRACTION = 0.25   # ¼-Kelly
DEFAULT_MAX_POSITION_PCT = 5.0  # hard per-position cap (% of capital)
DEFAULT_MAX_RISK_PCT = 1.0      # max capital risked to the stop (% of capital)


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
    p = max(0.0, min(1.0, win_prob))
    f = p - (1.0 - p) / b
    return max(0.0, f)


def kelly_position_size(
    capital: float,
    entry: float,
    stop: float,
    target: float,
    win_prob: float,
    *,
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
    }
    if capital <= 0 or entry <= 0:
        base["reason"] = "invalid capital/entry"
        return base

    b = payoff_ratio(entry, stop, target, direction)
    if b is None:
        base["reason"] = "invalid trade geometry (non-positive risk or reward)"
        return base
    base["payoff_ratio"] = round(b, 4)

    f_full = kelly_fraction(win_prob, b)
    base["kelly_f_full"] = round(f_full, 4)
    if f_full <= 0:
        base["reason"] = (
            f"non-positive Kelly edge (p={win_prob:.2f}, b={b:.2f}) — skip"
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
    }

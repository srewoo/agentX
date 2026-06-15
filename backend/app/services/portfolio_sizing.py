from __future__ import annotations
"""B2–B5 — portfolio-aware, regime-aware, drawdown-controlled sizing.

`kelly_sizing.kelly_position_size` sizes one trade in isolation. These pure
functions add the portfolio view that a per-trade Kelly can't see:

  * **B2 correlation-aware sizing** — shrink a new position the more it
    correlates with the open book, so we don't unknowingly stack five
    correlated bank shorts into one concentrated bet.
  * **B3 sector + exposure caps** — hard ceilings on per-sector weight and on
    gross/net directional exposure; a trade that would breach a cap is trimmed
    or dropped.
  * **B4 regime-aware dynamic Kelly fraction** — shrink the Kelly fraction in
    high-volatility regimes and after a losing streak; restore on recovery.
  * **B5 drawdown circuit-breaker** — halt new entries once portfolio drawdown
    breaches a floor, until it recovers.

All pure and deterministic — no DB, no clock — so they unit-test cleanly and
the caller supplies live state (open book, VIX, recent results, equity).
"""
from typing import Optional


# ── B2 — correlation-aware sizing ──
def correlation_size_multiplier(
    max_correlation: float, *, start: float = 0.5, floor: float = 0.3
) -> float:
    """Shrink factor in [floor, 1.0] from the max correlation to the open book.

    Below ``start`` correlation: full size (1.0). From ``start`` to 1.0,
    linearly shrink down to ``floor``. A hard veto above some threshold is the
    caller's job (kept separate from this graduated trim); this only scales.
    """
    c = max(0.0, min(1.0, max_correlation))
    if c <= start:
        return 1.0
    span = 1.0 - start
    frac = (c - start) / span if span > 0 else 1.0
    return max(floor, 1.0 - frac * (1.0 - floor))


# ── B3 — sector + gross/net exposure caps ──
def apply_exposure_caps(
    new_value: float,
    direction: str,
    *,
    capital: float,
    sector: str,
    sector_value_open: float,
    gross_open: float,
    net_open: float,
    max_sector_pct: float = 25.0,
    max_gross_pct: float = 150.0,
    max_net_pct: float = 100.0,
) -> dict:
    """Trim ``new_value`` so no cap is breached. Returns the allowed value.

    Signed net exposure: long adds +value, short adds −value. Caps are % of
    capital. Returns ``{allowed_value, binding, capped}``; allowed_value == 0
    means the trade can't be taken at all under current exposure.
    """
    if capital <= 0 or new_value <= 0:
        return {"allowed_value": 0.0, "binding": "invalid", "capped": True}

    sign = 1.0 if direction == "bullish" else -1.0
    limits = {
        "sector_cap": capital * max_sector_pct / 100.0 - sector_value_open,
        "gross_cap": capital * max_gross_pct / 100.0 - gross_open,
    }
    # Net cap binds only in the direction that increases |net|.
    net_room = capital * max_net_pct / 100.0 - sign * net_open
    limits["net_cap"] = net_room

    allowed = new_value
    binding = "none"
    for name, room in limits.items():
        room = max(0.0, room)
        if room < allowed:
            allowed = room
            binding = name
    allowed = max(0.0, round(allowed, 2))
    return {"allowed_value": allowed, "binding": binding, "capped": allowed < new_value}


# ── B4 — regime-aware dynamic Kelly fraction ──
def dynamic_kelly_fraction(
    base_fraction: float,
    *,
    vix: Optional[float] = None,
    recent_losses: int = 0,
    high_vix: float = 20.0,
    extreme_vix: float = 28.0,
    loss_streak_trigger: int = 3,
) -> float:
    """Scale the Kelly fraction down for risk, never up.

    High VIX halves the fraction; extreme VIX quarters it. A losing streak at or
    beyond ``loss_streak_trigger`` halves again (compounding). The result is
    clamped to (0, base_fraction] — these guards only ever reduce the bet.
    """
    f = max(0.0, base_fraction)
    if vix is not None:
        if vix >= extreme_vix:
            f *= 0.25
        elif vix >= high_vix:
            f *= 0.5
    if recent_losses >= loss_streak_trigger:
        f *= 0.5
    return min(base_fraction, f)


# ── B5 — drawdown circuit-breaker ──
def drawdown_breaker_tripped(
    peak_equity: float, current_equity: float, *, max_drawdown_pct: float = 15.0
) -> dict:
    """Halt new entries when drawdown from peak breaches the floor.

    Returns ``{tripped, drawdown_pct}``. ``tripped`` True ⇒ the caller should
    open no new positions (existing ones still managed) until equity recovers
    above the floor.
    """
    if peak_equity <= 0:
        return {"tripped": False, "drawdown_pct": 0.0}
    dd = max(0.0, (peak_equity - current_equity) / peak_equity * 100.0)
    return {"tripped": dd >= max_drawdown_pct, "drawdown_pct": round(dd, 3)}

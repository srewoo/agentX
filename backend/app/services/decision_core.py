from __future__ import annotations
"""2.2 — Event-driven decision core: decide(market_state) → orders.

The live entry logic (conviction floor, directional-concentration cap, Kelly
edge sizing, correlation gate, sector/gross/net exposure caps, the 10-rule risk
gate, and the drawdown breaker) used to live ONLY inside
``auto_paper_trader.auto_open_from_recommendations`` — tangled with DB reads and
data fetches, so the backtest could never exercise the same decisions. That is
the biggest source of backtest↔live drift: the backtest measured a different
system than the one that trades.

This module extracts that logic into a **pure, deterministic function**. All IO
(is-it-already-open, correlation-to-open, ATR/ADX, earnings) is pre-resolved by
the caller into the inputs below, so ``decide`` does no IO and is exhaustively
unit-testable. Two callers feed it:

  * **live** — ``auto_paper_trader`` builds the state from live fetches, then
    executes the returned orders (``create_paper_trade``);
  * **backtest** — a historical replay (2.3) builds the state from past bars and
    executes the orders against a simulated book.

Same function, same decisions. The backtest then IS the live system on old data.

``decide`` composes the already-pure helpers (``kelly_sizing``,
``portfolio_sizing``, ``risk_gate``) rather than reimplementing them, so there
is one definition of each rule.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EntryCandidate:
    """One actionable BUY/SELL candidate with all IO pre-resolved.

    Ordering across a batch is the caller's responsibility (live sorts by
    conviction desc, then risk:reward desc); ``decide`` consumes them in order.
    """
    symbol: str
    direction: str                 # "bullish" | "bearish"
    conviction: float
    risk_reward: float
    entry: float
    stop: float
    target: float
    sector: str = "Unknown"
    win_prob: float = 0.5          # calibrated p(win) for Kelly
    already_open: bool = False     # pre-resolved: same (symbol,direction) open?
    max_correlation: float = 0.0   # pre-resolved: corr to most-correlated open
    atr_pct: Optional[float] = None
    adx: Optional[float] = None
    data_quality: Optional[str] = None
    earnings_blackout: bool = False


@dataclass
class PortfolioCtx:
    """Mutable portfolio state; ``decide`` advances the running aggregates as it
    commits orders so caps are cumulative within one call (matches live)."""
    capital: float
    already_today: int = 0
    open_count: int = 0
    dir_counts: dict[str, int] = field(default_factory=lambda: {"bullish": 0, "bearish": 0})
    sector_open: dict[str, float] = field(default_factory=dict)
    gross_open: float = 0.0
    net_open: float = 0.0
    vix: Optional[float] = None
    recent_losses: int = 0
    peak_equity: Optional[float] = None
    cur_equity: Optional[float] = None
    open_positions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class DecisionConfig:
    min_conviction: float = 65
    max_per_day: int = 8
    max_open_positions: int = 30
    max_directional_concentration: float = 0.80
    min_positions_for_concentration: int = 3
    correlation_hard_reject: float = 0.7
    enable_risk_gate: bool = True


@dataclass
class Order:
    symbol: str
    direction: str
    conviction: float
    entry: float
    stop: float
    target: float
    shares: int
    position_size: float
    strength: int
    sizing: Optional[dict[str, Any]] = None
    max_correlation: float = 0.0


@dataclass
class SkipDecision:
    symbol: str
    direction: str
    reason: str
    sizing: Optional[dict[str, Any]] = None
    max_correlation: Optional[float] = None


@dataclass
class DecisionResult:
    orders: list[Order] = field(default_factory=list)
    skipped: list[SkipDecision] = field(default_factory=list)
    drawdown_tripped: bool = False
    drawdown_pct: float = 0.0

    @property
    def skip_reason_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.skipped:
            key = s.reason.split(":")[0]
            out[key] = out.get(key, 0) + 1
        return out


def _would_exceed_concentration(
    direction: str, dir_counts: dict[str, int], cfg: DecisionConfig
) -> bool:
    """True if opening one more ``direction`` position would push the OPEN book
    past the directional-concentration cap. Mirrors auto_paper_trader."""
    if cfg.max_directional_concentration >= 1.0:
        return False
    proj_dir = dir_counts.get(direction, 0) + 1
    proj_total = dir_counts["bullish"] + dir_counts["bearish"] + 1
    if proj_total < max(1, cfg.min_positions_for_concentration):
        return False
    return (proj_dir / proj_total) > cfg.max_directional_concentration


def decide(
    candidates: list[EntryCandidate], ctx: PortfolioCtx, cfg: DecisionConfig,
) -> DecisionResult:
    """Pure entry-decision function. Given ordered candidates + portfolio state,
    return the orders to open and the skip decisions (with reasons), advancing
    ``ctx`` aggregates as orders commit. No IO — deterministic and testable.

    The rule ORDER and skip-reason strings match
    ``auto_paper_trader.auto_open_from_recommendations`` so the two stay in lockstep.
    """
    from app.services.kelly_sizing import kelly_position_size, per_position_cap_pct
    from app.services.portfolio_sizing import (
        apply_exposure_caps, correlation_size_multiplier, dynamic_kelly_fraction,
        drawdown_breaker_tripped,
    )

    res = DecisionResult()

    # ── B5: drawdown breaker halts ALL new entries ──
    if ctx.peak_equity is not None and ctx.cur_equity is not None:
        breaker = drawdown_breaker_tripped(ctx.peak_equity, ctx.cur_equity)
        if breaker["tripped"]:
            res.drawdown_tripped = True
            res.drawdown_pct = breaker["drawdown_pct"]
            for c in candidates:
                res.skipped.append(SkipDecision(c.symbol, c.direction, "drawdown_breaker"))
            return res

    # B4: regime/streak-aware Kelly fraction (once per call).
    from app.services.kelly_sizing import DEFAULT_KELLY_FRACTION
    dyn_fraction = dynamic_kelly_fraction(
        DEFAULT_KELLY_FRACTION, vix=ctx.vix, recent_losses=ctx.recent_losses)
    pos_cap_pct = per_position_cap_pct(cfg.max_open_positions)

    remaining_daily = max(0, cfg.max_per_day - ctx.already_today)
    remaining_open = max(0, cfg.max_open_positions - ctx.open_count)
    budget = min(remaining_daily, remaining_open)

    committed: set[tuple[str, str]] = set()  # intra-call dedup (mirrors live DB write)
    for c in candidates:
        if len(res.orders) >= budget:
            res.skipped.append(SkipDecision(c.symbol, c.direction, "budget_exhausted"))
            continue
        if c.already_open or (c.symbol, c.direction) in committed:
            res.skipped.append(SkipDecision(c.symbol, c.direction, "already_open"))
            continue
        if _would_exceed_concentration(c.direction, ctx.dir_counts, cfg):
            res.skipped.append(SkipDecision(c.symbol, c.direction, "directional_concentration_cap"))
            continue
        if c.entry <= 0 or c.stop <= 0 or c.target <= 0:
            res.skipped.append(SkipDecision(c.symbol, c.direction, "invalid_prices"))
            continue

        # ── edge-aware Kelly sizing (the filter) ──
        sizing = kelly_position_size(
            capital=ctx.capital, entry=c.entry, stop=c.stop, target=c.target,
            win_prob=c.win_prob, direction=c.direction,
            kelly_fraction_mult=dyn_fraction, max_position_pct=pos_cap_pct,
        )
        if sizing["skip"]:
            res.skipped.append(SkipDecision(c.symbol, c.direction, "negative_kelly_edge", sizing=sizing))
            continue
        kelly_shares = int(sizing["shares"])
        position_size = float(sizing["position_value"])
        strength = max(1, min(10, int(c.conviction // 10)))

        # ── hard correlation reject ──
        if c.max_correlation >= cfg.correlation_hard_reject:
            res.skipped.append(SkipDecision(
                c.symbol, c.direction, "correlated_to_open",
                sizing=sizing, max_correlation=c.max_correlation))
            continue

        # ── graduated correlation trim + sector/gross/net exposure caps ──
        corr_mult = correlation_size_multiplier(c.max_correlation)
        capped = apply_exposure_caps(
            position_size * corr_mult, c.direction, capital=ctx.capital,
            sector=c.sector, sector_value_open=ctx.sector_open.get(c.sector, 0.0),
            gross_open=ctx.gross_open, net_open=ctx.net_open,
        )
        new_shares = int(capped["allowed_value"] / c.entry) if c.entry > 0 else 0
        if new_shares <= 0:
            res.skipped.append(SkipDecision(
                c.symbol, c.direction, f"exposure_capped:{capped['binding']}",
                sizing=sizing, max_correlation=c.max_correlation))
            continue
        if new_shares < kelly_shares:
            kelly_shares = new_shares
            position_size = round(kelly_shares * c.entry, 2)

        # ── 10-rule risk gate ──
        if cfg.enable_risk_gate:
            gate = _run_risk_gate(c, ctx, kelly_shares)
            if gate is not None:
                if gate["rejected"]:
                    res.skipped.append(SkipDecision(
                        c.symbol, c.direction, "risk_gate_rejected: " + gate["reasons"],
                        sizing=sizing, max_correlation=c.max_correlation))
                    continue
                if gate["modified_qty"] is not None:
                    kelly_shares = max(0, min(kelly_shares, gate["modified_qty"]))
                    if kelly_shares <= 0:
                        res.skipped.append(SkipDecision(
                            c.symbol, c.direction, "risk_gate_trimmed_to_zero",
                            sizing=sizing, max_correlation=c.max_correlation))
                        continue
                    position_size = round(kelly_shares * c.entry, 2)
                    strength = max(1, min(strength, gate["modified_qty"]))

        # ── commit the order; advance running aggregates (cumulative caps) ──
        res.orders.append(Order(
            symbol=c.symbol, direction=c.direction, conviction=c.conviction,
            entry=c.entry, stop=c.stop, target=c.target, shares=kelly_shares,
            position_size=position_size, strength=strength, sizing=sizing,
            max_correlation=c.max_correlation))
        if c.direction in ctx.dir_counts:
            ctx.dir_counts[c.direction] += 1
        if position_size:
            ctx.gross_open += position_size
            ctx.net_open += position_size if c.direction == "bullish" else -position_size
            ctx.sector_open[c.sector] = ctx.sector_open.get(c.sector, 0.0) + position_size

    return res


def _run_risk_gate(c: EntryCandidate, ctx: PortfolioCtx, qty: int) -> Optional[dict[str, Any]]:
    """Adapt the pure risk_gate to the decision core. Returns
    ``{rejected, reasons, modified_qty}`` or None if the gate is unavailable."""
    try:
        from app.services.risk_gate import (
            TradeCandidate, PortfolioState, evaluate_trade, GateVerdict,
        )
    except Exception:
        return None
    candidate = TradeCandidate(
        symbol=c.symbol, direction=c.direction, entry=c.entry, stop=c.stop,
        target=c.target, qty=max(1, qty), sector=c.sector,
        data_quality=c.data_quality, atr_pct=c.atr_pct, adx=c.adx,
        is_in_earnings_blackout=c.earnings_blackout,
    )
    portfolio = PortfolioState(
        capital=ctx.capital, open_positions=ctx.open_positions,
        correlation_to_open=c.max_correlation,
    )
    gate = evaluate_trade(candidate, portfolio)
    return {
        "rejected": gate.verdict == GateVerdict.REJECTED,
        "reasons": "; ".join(gate.reasons),
        "modified_qty": gate.modified_qty if gate.verdict == GateVerdict.MODIFIED else None,
    }

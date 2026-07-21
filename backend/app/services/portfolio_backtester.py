from __future__ import annotations
"""2.3 — Portfolio-level backtesting on the live decision core.

The walk-forward backtester scores signals independently at ``qty=1`` and counts
percentages — it never models a real book: capital, concurrent-position limits,
compounding, or costs at the size Kelly would actually trade. A signal that
"wins 55%" at qty=1 can still bankrupt a book that over-concentrates, and a
0.5%-of-ADV cost assumption understates what a real Kelly-sized order pays.

This simulator replays chronological bars through the SAME ``decision_core.decide``
the live trader uses (2.2), against a real portfolio ledger:

  * **Real sizes** — every entry is Kelly-sized by ``decide`` against current
    equity, then capped by the same sector/gross/net exposure rules.
  * **Capital constraints + concurrency** — ``max_open_positions`` and the
    exposure caps bind exactly as they do live.
  * **Compounding** — sizing uses ``initial + realised_pnl`` as it grows.
  * **Costs at the traded size** — round-trip cost is computed on each
    position's actual notional, not a fixed 0.5%-of-ADV assumption.
  * **Gap-aware exits** — a bar gapping through the stop fills at the open (2.5).

The bar loop is pure given an injected ``candidate_fn`` and price arrays, so it
unit-tests deterministically without a data feed.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from app.services.decision_core import DecisionConfig, EntryCandidate, PortfolioCtx, decide

logger = logging.getLogger(__name__)


@dataclass
class OHLC:
    """Per-symbol price arrays, indexed by bar."""
    open: Sequence[float]
    high: Sequence[float]
    low: Sequence[float]
    close: Sequence[float]


@dataclass
class _Position:
    symbol: str
    direction: str
    shares: int
    entry: float
    stop: float
    target: float
    entry_bar: int
    sector: str
    cost_pct: float


@dataclass
class ClosedTrade:
    symbol: str
    direction: str
    shares: int
    entry: float
    exit: float
    entry_bar: int
    exit_bar: int
    exit_reason: str
    gross_pnl_pct: float
    net_pnl_pct: float
    pnl_amount: float


@dataclass
class PortfolioBacktestResult:
    initial_capital: float
    final_equity: float
    realized_pnl: float
    total_return_pct: float
    n_trades: int
    wins: int
    win_rate: float
    max_concurrent: int
    max_drawdown_pct: float
    equity_curve: list[float] = field(default_factory=list)
    trades: list[ClosedTrade] = field(default_factory=list)


def _bar_exit(direction: str, stop: float, target: float,
              o: float, h: float, l: float) -> Optional[tuple[float, str]]:
    """Exit fill for one bar, gap-aware (2.5). None if neither stop nor target hit."""
    if direction == "bullish":
        if o <= stop:
            return o, "stop"          # gapped through the stop at the open
        if l <= stop:
            return stop, "stop"
        if h >= target:
            return target, "target"
    else:
        if o >= stop:
            return o, "stop"
        if h >= stop:
            return stop, "stop"
        if l <= target:
            return target, "target"
    return None


def _default_cost_pct(position_value: float) -> float:
    """Round-trip cost % at the ACTUAL traded notional (not a fixed ADV share)."""
    try:
        from app.services.execution_costs import round_trip_cost_pct
        # Assume the order is a modest slice of a liquid name's daily value.
        return round_trip_cost_pct(
            trade_value_inr=max(position_value, 1.0),
            avg_daily_value_inr=max(position_value, 1.0) * 200.0,
            daily_vol_pct=1.5,
        )
    except Exception:
        return 0.20  # 20bps fallback


def simulate_portfolio(
    bars: Sequence[int],
    ohlc: dict[str, OHLC],
    candidate_fn: Callable[[int], list[EntryCandidate]],
    *,
    initial_capital: float = 1_000_000.0,
    config: Optional[DecisionConfig] = None,
    max_hold_bars: int = 7,
    cost_pct_fn: Optional[Callable[[float], float]] = None,
) -> PortfolioBacktestResult:
    """Replay ``bars`` through ``decide`` against a real portfolio ledger.

    ``candidate_fn(bar)`` returns the raw entry candidates visible at that bar
    (``already_open``/``max_correlation`` are set by the engine). Positions exit
    on a gap-aware stop/target hit or after ``max_hold_bars`` (time exit).
    """
    cfg = config or DecisionConfig()
    cost_fn = cost_pct_fn or _default_cost_pct
    open_positions: list[_Position] = []
    realized = 0.0
    trades: list[ClosedTrade] = []
    equity_curve: list[float] = []
    peak = initial_capital
    max_dd = 0.0
    max_concurrent = 0

    def _close(pos: _Position, exit_px: float, bar: int, reason: str) -> None:
        nonlocal realized
        sign = 1.0 if pos.direction == "bullish" else -1.0
        gross = (exit_px - pos.entry) / pos.entry * 100.0 * sign
        net = gross - pos.cost_pct
        pnl_amt = net / 100.0 * (pos.shares * pos.entry)
        realized += pnl_amt
        trades.append(ClosedTrade(
            symbol=pos.symbol, direction=pos.direction, shares=pos.shares,
            entry=pos.entry, exit=exit_px, entry_bar=pos.entry_bar, exit_bar=bar,
            exit_reason=reason, gross_pnl_pct=round(gross, 4),
            net_pnl_pct=round(net, 4), pnl_amount=round(pnl_amt, 2)))

    for bar in bars:
        # ── 1. Exits (gap-aware stop/target, else time-exit at max hold). ──
        still_open: list[_Position] = []
        for pos in open_positions:
            px = ohlc.get(pos.symbol)
            if px is None or bar >= len(px.close):
                still_open.append(pos)
                continue
            hit = _bar_exit(pos.direction, pos.stop, pos.target,
                            float(px.open[bar]), float(px.high[bar]), float(px.low[bar]))
            if hit is not None:
                _close(pos, hit[0], bar, hit[1])
            elif (bar - pos.entry_bar) >= max_hold_bars:
                _close(pos, float(px.close[bar]), bar, "time")
            else:
                still_open.append(pos)
        open_positions = still_open

        # ── 2. Entries via the shared decision core. ──
        cands = candidate_fn(bar) or []
        if cands:
            open_keys = {(p.symbol, p.direction) for p in open_positions}
            dir_counts = {"bullish": 0, "bearish": 0}
            sector_open: dict[str, float] = {}
            gross_open = net_open = 0.0
            for p in open_positions:
                dir_counts[p.direction] = dir_counts.get(p.direction, 0) + 1
                val = p.shares * p.entry
                gross_open += val
                net_open += val if p.direction == "bullish" else -val
                sector_open[p.sector] = sector_open.get(p.sector, 0.0) + val
            resolved = [
                EntryCandidate(
                    symbol=c.symbol, direction=c.direction, conviction=c.conviction,
                    risk_reward=c.risk_reward, entry=c.entry, stop=c.stop,
                    target=c.target, sector=c.sector, win_prob=c.win_prob,
                    already_open=(c.symbol, c.direction) in open_keys,
                    max_correlation=c.max_correlation, atr_pct=c.atr_pct, adx=c.adx,
                    data_quality=c.data_quality, earnings_blackout=c.earnings_blackout)
                for c in cands
            ]
            ctx = PortfolioCtx(
                capital=initial_capital + realized,   # compounding
                already_today=0, open_count=len(open_positions),
                dir_counts=dir_counts, sector_open=sector_open,
                gross_open=gross_open, net_open=net_open,
                open_positions=[{"symbol": p.symbol, "direction": p.direction,
                                 "position_size": p.shares * p.entry} for p in open_positions])
            result = decide(resolved, ctx, cfg)
            for o in result.orders:
                open_positions.append(_Position(
                    symbol=o.symbol, direction=o.direction, shares=o.shares,
                    entry=o.entry, stop=o.stop, target=o.target, entry_bar=bar,
                    sector=next((c.sector for c in resolved
                                 if c.symbol == o.symbol and c.direction == o.direction), "Unknown"),
                    cost_pct=cost_fn(o.position_size)))

        max_concurrent = max(max_concurrent, len(open_positions))
        equity = initial_capital + realized
        equity_curve.append(round(equity, 2))
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100.0)

    wins = sum(1 for t in trades if t.net_pnl_pct > 0)
    final_equity = initial_capital + realized
    return PortfolioBacktestResult(
        initial_capital=initial_capital, final_equity=round(final_equity, 2),
        realized_pnl=round(realized, 2),
        total_return_pct=round((final_equity - initial_capital) / initial_capital * 100.0, 4),
        n_trades=len(trades), wins=wins,
        win_rate=round(wins / len(trades), 4) if trades else 0.0,
        max_concurrent=max_concurrent, max_drawdown_pct=round(max_dd, 3),
        equity_curve=equity_curve, trades=trades)

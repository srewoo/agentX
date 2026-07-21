from __future__ import annotations
"""2.3 — portfolio-level backtest on the shared decision core."""
import pytest

from app.services.decision_core import DecisionConfig, EntryCandidate
from app.services.portfolio_backtester import OHLC, simulate_portfolio


def _flat(price: float, n: int) -> list[float]:
    return [price] * n


def _cfg(**kw):
    base = dict(min_conviction=65, max_per_day=8, max_open_positions=30,
                enable_risk_gate=False, max_directional_concentration=1.0)
    base.update(kw)
    return DecisionConfig(**base)


def _long(symbol="INFY", entry=100.0, stop=95.0, target=115.0, sector="IT"):
    return EntryCandidate(symbol=symbol, direction="bullish", conviction=80,
                          risk_reward=3.0, entry=entry, stop=stop, target=target,
                          sector=sector, win_prob=0.58)


def test_winning_trade_compounds_equity():
    # Enter at bar 0 (price 100); bar 2 rallies through the 115 target → win.
    high = [100, 100, 120, 120]
    ohlc = {"INFY": OHLC(open=_flat(100, 4), high=high, low=_flat(99, 4), close=[100, 100, 118, 118])}

    def cand_fn(bar):
        return [_long()] if bar == 0 else []

    res = simulate_portfolio([0, 1, 2, 3], ohlc, cand_fn,
                             initial_capital=1_000_000, config=_cfg(), max_hold_bars=7)
    assert res.n_trades == 1
    assert res.trades[0].exit_reason == "target"
    assert res.final_equity > res.initial_capital     # compounded up
    assert res.total_return_pct > 0


def test_gap_down_through_stop_fills_at_open():
    # Bar 1 opens at 90 — gaps below the 95 stop; fill at 90, not 95.
    ohlc = {"INFY": OHLC(open=[100, 90], high=[101, 92], low=[99, 88], close=[100, 91])}

    def cand_fn(bar):
        return [_long()] if bar == 0 else []

    res = simulate_portfolio([0, 1], ohlc, cand_fn, config=_cfg(),
                             cost_pct_fn=lambda v: 0.0)
    t = res.trades[0]
    assert t.exit == 90.0 and t.exit_reason == "stop"
    # Loss ≈ (90-100)/100 = -10%.
    assert t.net_pnl_pct == pytest.approx(-10.0, abs=1e-6)


def test_time_exit_after_max_hold():
    ohlc = {"INFY": OHLC(open=_flat(100, 5), high=_flat(101, 5), low=_flat(99, 5),
                         close=[100, 100, 100, 100, 103])}

    def cand_fn(bar):
        return [_long()] if bar == 0 else []

    res = simulate_portfolio([0, 1, 2, 3, 4], ohlc, cand_fn, config=_cfg(), max_hold_bars=3)
    assert res.trades[0].exit_reason == "time"
    assert res.trades[0].exit_bar == 3           # entry_bar 0 + 3


def test_concurrent_position_limit_binds():
    # 5 names all signal at bar 0, but max_open_positions=2 caps the book.
    syms = [f"S{i}" for i in range(5)]
    ohlc = {s: OHLC(open=_flat(100, 3), high=_flat(101, 3), low=_flat(99, 3),
                    close=_flat(100, 3)) for s in syms}

    def cand_fn(bar):
        return [_long(symbol=s, sector=s) for s in syms] if bar == 0 else []

    res = simulate_portfolio([0, 1, 2], ohlc, cand_fn,
                             config=_cfg(max_open_positions=2), max_hold_bars=7)
    assert res.max_concurrent == 2               # never more than the cap open


def test_costs_reduce_net_pnl():
    ohlc = {"INFY": OHLC(open=[100, 100, 120], high=[100, 100, 120],
                         low=[99, 99, 119], close=[100, 100, 118])}

    def cand_fn(bar):
        return [_long()] if bar == 0 else []

    free = simulate_portfolio([0, 1, 2], ohlc, cand_fn, config=_cfg(), cost_pct_fn=lambda v: 0.0)
    costed = simulate_portfolio([0, 1, 2], ohlc, cand_fn, config=_cfg(), cost_pct_fn=lambda v: 0.5)
    assert costed.trades[0].net_pnl_pct == pytest.approx(free.trades[0].net_pnl_pct - 0.5, abs=1e-6)


def test_no_signals_no_trades():
    ohlc = {"INFY": OHLC(open=_flat(100, 3), high=_flat(101, 3), low=_flat(99, 3), close=_flat(100, 3))}
    res = simulate_portfolio([0, 1, 2], ohlc, lambda bar: [], config=_cfg())
    assert res.n_trades == 0
    assert res.final_equity == res.initial_capital

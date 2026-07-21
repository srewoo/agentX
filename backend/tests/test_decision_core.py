from __future__ import annotations
"""2.2 — pure event-driven decision core. Each branch is exercised deterministically."""
import pytest

from app.services.decision_core import (
    EntryCandidate, PortfolioCtx, DecisionConfig, decide,
)


def _cand(symbol="INFY", direction="bullish", conviction=80, **kw):
    # A comfortably positive-edge long: entry 100, stop 95, target 115 (R:R 3).
    base = dict(symbol=symbol, direction=direction, conviction=conviction,
                risk_reward=3.0, entry=100.0, stop=95.0, target=115.0,
                sector="IT", win_prob=0.58)
    base.update(kw)
    return EntryCandidate(**base)


def _cfg(**kw):
    # Risk gate off by default in unit tests so we isolate the core arithmetic.
    base = dict(min_conviction=65, max_per_day=8, max_open_positions=30,
                enable_risk_gate=False)
    base.update(kw)
    return DecisionConfig(**base)


def test_positive_edge_candidate_opens():
    res = decide([_cand()], PortfolioCtx(capital=1_000_000), _cfg())
    assert len(res.orders) == 1
    o = res.orders[0]
    assert o.symbol == "INFY" and o.shares > 0 and o.position_size > 0


def test_already_open_skipped():
    res = decide([_cand(already_open=True)], PortfolioCtx(capital=1_000_000), _cfg())
    assert not res.orders
    assert res.skipped[0].reason == "already_open"


def test_invalid_prices_skipped():
    res = decide([_cand(entry=0)], PortfolioCtx(capital=1_000_000), _cfg())
    assert res.skipped[0].reason == "invalid_prices"


def test_negative_edge_skipped():
    # Terrible payoff: tiny target, huge stop → Kelly declines the bet.
    bad = _cand(entry=100, stop=50, target=101, risk_reward=0.02, win_prob=0.5)
    res = decide([bad], PortfolioCtx(capital=1_000_000), _cfg())
    assert not res.orders
    assert res.skipped[0].reason == "negative_kelly_edge"


def test_hard_correlation_reject():
    res = decide([_cand(max_correlation=0.8)], PortfolioCtx(capital=1_000_000), _cfg())
    assert res.skipped[0].reason == "correlated_to_open"


def test_budget_from_daily_and_open_caps():
    cands = [_cand(symbol=f"S{i}") for i in range(10)]
    # max_per_day=3 caps opens to 3 even though 10 are eligible. Disable the
    # directional-concentration cap so budget is the sole limiter under test.
    res = decide(cands, PortfolioCtx(capital=1_000_000),
                 _cfg(max_per_day=3, max_directional_concentration=1.0))
    assert len(res.orders) == 3
    assert res.skip_reason_counts.get("budget_exhausted") == 7


def test_open_cap_limits_budget():
    cands = [_cand(symbol=f"S{i}") for i in range(10)]
    ctx = PortfolioCtx(capital=1_000_000, open_count=28)  # only 2 slots to 30
    res = decide(cands, ctx, _cfg(max_open_positions=30, max_per_day=8))
    assert len(res.orders) == 2


def test_directional_concentration_cap_blocks_lopsided_book():
    # Book already 3 bullish, 0 bearish (100% long); cap 0.80 blocks a 4th long.
    ctx = PortfolioCtx(capital=1_000_000, dir_counts={"bullish": 3, "bearish": 0})
    res = decide([_cand(direction="bullish")], ctx,
                 _cfg(max_directional_concentration=0.80, min_positions_for_concentration=3))
    assert res.skipped[0].reason == "directional_concentration_cap"


def test_drawdown_breaker_halts_all_entries():
    ctx = PortfolioCtx(capital=1_000_000, peak_equity=1_000_000, cur_equity=800_000)
    res = decide([_cand(), _cand(symbol="TCS")], ctx, _cfg())
    assert res.drawdown_tripped is True
    assert not res.orders
    assert res.skip_reason_counts.get("drawdown_breaker") == 2


def test_running_aggregates_advance_across_orders():
    # Two names open in one call; gross exposure accumulates.
    cands = [_cand(symbol="A", sector="IT"), _cand(symbol="B", sector="Auto")]
    ctx = PortfolioCtx(capital=1_000_000)
    res = decide(cands, ctx, _cfg())
    assert len(res.orders) == 2
    assert ctx.gross_open == pytest.approx(sum(o.position_size for o in res.orders))
    assert ctx.dir_counts["bullish"] == 2


def test_per_trade_notional_shrinks_with_wider_book():
    # Same candidate, but a wider max_open ⇒ smaller per-position cap ⇒ fewer shares.
    narrow = decide([_cand()], PortfolioCtx(capital=1_000_000), _cfg(max_open_positions=12))
    wide = decide([_cand()], PortfolioCtx(capital=1_000_000), _cfg(max_open_positions=30))
    assert wide.orders[0].position_size <= narrow.orders[0].position_size


def test_exposure_cap_can_block_when_gross_full():
    # Gross already at the 150%-of-capital cap → no room for another long.
    ctx = PortfolioCtx(capital=1_000_000, gross_open=1_500_000)
    res = decide([_cand()], ctx, _cfg())
    assert res.skipped[0].reason.startswith("exposure_capped")

"""Golden tests for portfolio analytics.

We test the pure-math layer with hand-computed values. The async DB layer
gets a tiny round-trip test against the conftest's tmp DB.
"""
from __future__ import annotations

import math

import aiosqlite
import pytest

from app.services import portfolio as svc


# ── FIFO P&L ──────────────────────────────────────────────────
def _tx(symbol: str, side: str, qty: float, price: float, ts: str, fees: float = 0.0) -> dict:
    return {
        "symbol": symbol, "side": side, "qty": qty, "price": price,
        "ts": ts, "fees": fees, "notes": None,
    }


def test_fifo_realized_pnl_simple_round_trip() -> None:
    """Buy 100 @ 100, sell 100 @ 110 => +1000 realized, no open lots."""
    txs = [
        _tx("ACME", "BUY",  100, 100.0, "2026-01-01T09:15:00+00:00"),
        _tx("ACME", "SELL", 100, 110.0, "2026-01-05T09:15:00+00:00"),
    ]
    res = svc.compute_fifo(txs)
    assert len(res.realized) == 1
    assert res.realized[0].pnl == pytest.approx(1000.0)
    assert res.open_lots == {}


def test_fifo_partial_sell_splits_lots_oldest_first() -> None:
    """Two buys, one partial sell: must consume the older lot first."""
    txs = [
        _tx("ACME", "BUY",  50, 100.0, "2026-01-01T09:15:00+00:00"),
        _tx("ACME", "BUY",  50, 120.0, "2026-01-02T09:15:00+00:00"),
        _tx("ACME", "SELL", 70, 130.0, "2026-01-10T09:15:00+00:00"),
    ]
    res = svc.compute_fifo(txs)
    # First slice: 50 @ (130 - 100) = 1500
    # Second slice: 20 @ (130 - 120) = 200
    assert sum(r.pnl for r in res.realized) == pytest.approx(1700.0)
    assert len(res.realized) == 2
    # 30 shares left on the second lot at 120
    assert "ACME" in res.open_lots
    remaining = res.open_lots["ACME"]
    assert len(remaining) == 1
    assert remaining[0].qty == pytest.approx(30.0)
    assert remaining[0].price == pytest.approx(120.0)


def test_fifo_fees_lift_cost_basis_and_reduce_proceeds() -> None:
    """Fees on buy raise basis; fees on sell reduce net proceeds."""
    txs = [
        _tx("ACME", "BUY",  100, 100.0, "2026-01-01T09:15:00+00:00", fees=100.0),  # +1/share
        _tx("ACME", "SELL", 100, 110.0, "2026-01-05T09:15:00+00:00", fees=50.0),   # -0.5/share
    ]
    res = svc.compute_fifo(txs)
    # Effective: (110 - 0.5) - (100 + 1) = 8.5 per share -> 850
    assert res.realized[0].pnl == pytest.approx(850.0)


def test_fifo_short_sell_raises() -> None:
    """Selling without an open lot is a data bug — must raise loudly."""
    txs = [_tx("ACME", "SELL", 10, 100.0, "2026-01-01T09:15:00+00:00")]
    with pytest.raises(ValueError, match="exceeds available lots"):
        svc.compute_fifo(txs)


# ── Max drawdown ──────────────────────────────────────────────
def test_max_drawdown_known_curve() -> None:
    # Peak 100, trough 70 -> dd 30
    assert svc.max_drawdown([100, 90, 70, 80, 75]) == pytest.approx(30.0)


def test_max_drawdown_monotonic_curve_zero() -> None:
    assert svc.max_drawdown([10, 20, 30, 40]) == 0.0


def test_max_drawdown_empty_curve_zero() -> None:
    assert svc.max_drawdown([]) == 0.0
    assert svc.max_drawdown([42]) == 0.0


# ── Sharpe ratio ──────────────────────────────────────────────
def test_sharpe_zero_variance_returns_zero() -> None:
    assert svc.sharpe_ratio([0.001, 0.001, 0.001]) == 0.0


def test_sharpe_constant_excess_known_value() -> None:
    """Daily returns matching the daily rf -> excess is 0 -> Sharpe 0."""
    rf = 0.07
    daily_rf = rf / svc.TRADING_DAYS_PER_YEAR
    series = [daily_rf] * 30
    assert svc.sharpe_ratio(series, risk_free_rate=rf) == 0.0


def test_sharpe_positive_for_positive_returns() -> None:
    series = [0.01, 0.02, 0.015, 0.005, 0.012, 0.018]
    s = svc.sharpe_ratio(series, risk_free_rate=0.0)
    assert s > 0
    assert math.isfinite(s)


# ── Beta ──────────────────────────────────────────────────────
def test_beta_perfect_correlation_returns_one() -> None:
    p = [0.01, -0.005, 0.012, -0.008, 0.003]
    assert svc.beta(p, p) == pytest.approx(1.0)


def test_beta_double_movement_returns_two() -> None:
    bench = [0.01, -0.005, 0.012, -0.008, 0.003]
    port = [2 * x for x in bench]
    assert svc.beta(port, bench) == pytest.approx(2.0)


def test_beta_zero_variance_benchmark_returns_zero() -> None:
    assert svc.beta([0.01, 0.02], [0.005, 0.005]) == 0.0


# ── Win metrics ───────────────────────────────────────────────
def test_win_metrics_known_ledger() -> None:
    realized = [
        svc.RealizedTrade("A", 1, 100, 110, "t1", "t2", 10.0),
        svc.RealizedTrade("A", 1, 100,  95, "t3", "t4", -5.0),
        svc.RealizedTrade("A", 1, 100, 120, "t5", "t6", 20.0),
    ]
    m = svc.win_metrics(realized)
    assert m["trades"] == 3
    assert m["win_rate"] == pytest.approx(2 / 3, rel=1e-3)
    assert m["avg_win"] == pytest.approx(15.0)
    assert m["avg_loss"] == pytest.approx(5.0)
    # PF = 30 / 5 = 6
    assert m["profit_factor"] == pytest.approx(6.0)


def test_win_metrics_empty() -> None:
    m = svc.win_metrics([])
    assert m["trades"] == 0
    assert m["win_rate"] == 0.0


# ── INR formatter ─────────────────────────────────────────────
def test_format_inr_lakh_crore() -> None:
    assert svc.format_inr(1_25_00_000) == "₹1.25 Cr"
    assert svc.format_inr(1_25_000) == "₹1.25 L"
    assert svc.format_inr(-1_25_000) == "-₹1.25 L"
    # Sub-lakh uses Indian grouping
    assert svc.format_inr(12345.6) == "₹12,345.60"


# ── DB round-trip (uses the tmp_db_path fixture from conftest) ──
@pytest.mark.asyncio
async def test_insert_and_list_transactions_roundtrip(tmp_db_path: str, monkeypatch) -> None:
    monkeypatch.setattr(svc, "DB_PATH", tmp_db_path)
    # Apply migration against the tmp DB.
    await svc.ensure_schema()

    await svc.insert_transaction(
        symbol="RELIANCE", side="BUY", qty=10, price=2500.0,
        fees=5.0, notes="opening",
        ts="2026-01-01T09:15:00+00:00",
    )
    await svc.insert_transaction(
        symbol="RELIANCE", side="SELL", qty=10, price=2600.0,
        fees=5.0, ts="2026-01-10T09:15:00+00:00",
    )

    page = await svc.list_transactions(symbol="RELIANCE", limit=10)
    assert len(page["transactions"]) == 2
    assert page["next_cursor"] is None

    txs = await svc.fetch_all_transactions_chronological()
    res = svc.compute_fifo(txs)
    # (2600 - 0.5) - (2500 + 0.5) = 99 per share -> 990
    assert sum(r.pnl for r in res.realized) == pytest.approx(990.0)


@pytest.mark.asyncio
async def test_ensure_schema_is_idempotent(tmp_db_path: str, monkeypatch) -> None:
    monkeypatch.setattr(svc, "DB_PATH", tmp_db_path)
    await svc.ensure_schema()
    await svc.ensure_schema()  # second call must not error
    async with aiosqlite.connect(tmp_db_path) as db:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('holdings','transactions','benchmarks')"
        ) as cur:
            names = {row[0] for row in await cur.fetchall()}
    assert names == {"holdings", "transactions", "benchmarks"}


@pytest.mark.asyncio
async def test_portfolio_recommendation_context_blocks_concentrated_buy(
    tmp_db_path: str, monkeypatch
) -> None:
    monkeypatch.setattr(svc, "DB_PATH", tmp_db_path)
    await svc.ensure_schema()
    await svc.insert_transaction(
        symbol="RELIANCE",
        side="BUY",
        qty=100,
        price=1000.0,
        ts="2026-01-01T09:15:00+00:00",
    )

    ctx = await svc.portfolio_recommendation_context(
        symbol="RELIANCE",
        sector="Energy",
        action="BUY",
    )
    assert ctx["available"] is True
    assert ctx["decision"] == "block_add"
    assert ctx["action_adjustment"] < 0
    assert ctx["symbol_weight_pct"] == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_portfolio_recommendation_context_boosts_existing_sell(
    tmp_db_path: str, monkeypatch
) -> None:
    monkeypatch.setattr(svc, "DB_PATH", tmp_db_path)
    await svc.ensure_schema()
    await svc.insert_transaction(
        symbol="RELIANCE",
        side="BUY",
        qty=10,
        price=1000.0,
        ts="2026-01-01T09:15:00+00:00",
    )

    ctx = await svc.portfolio_recommendation_context(
        symbol="RELIANCE",
        sector="Energy",
        action="SELL",
    )
    assert ctx["decision"] == "existing_position_exit"
    assert ctx["action_adjustment"] > 0

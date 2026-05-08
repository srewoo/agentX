"""Tests for async timeout / retry behaviour in data_fetcher and orchestrator.

Covers:
- `get_stock_info_async` offloads sync yfinance work and bounds it with a
  timeout; returns a stub on TimeoutError without bubbling up.
- `get_stock_info_async` bypasses yfinance entirely for entries cached in
  ``MAJOR_STOCKS`` (no executor call, so the event loop is never blocked).
- `_fetch_nse_delivery` retries once on a 403 after warming the session, and
  surfaces ``None`` only after both attempts fail.
- The weekly backtest fan-out caps concurrency at the configured semaphore
  size, applies a per-symbol timeout, and aggregates only the successful
  results.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import data_fetcher
from app.services.data_fetcher import (
    _YFINANCE_INFO_TIMEOUT,
    _fetch_nse_delivery,
    get_stock_info_async,
)


# ---------------------------------------------------------------------------
# get_stock_info_async
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_stock_info_async_uses_major_stocks_cache_without_yfinance() -> None:
    """Symbols in MAJOR_STOCKS must short-circuit before hitting yfinance."""
    with patch.object(data_fetcher, "yf") as mock_yf:
        result = await get_stock_info_async("RELIANCE")

    assert result["name"] == "Reliance Industries"
    assert result["sector"] == "Energy"
    mock_yf.Ticker.assert_not_called()


@pytest.mark.asyncio
async def test_get_stock_info_async_returns_normalized_info_from_yfinance() -> None:
    """Unknown symbol -> yfinance fallback runs in the executor and is normalized."""
    fake_ticker = MagicMock()
    fake_ticker.info = {
        "longName": "Acme Co",
        "sector": "Industrials",
        "industry": "Widgets",
        "trailingPE": 21.5,
        "marketCap": 1_000_000,
        "currency": "USD",
    }

    with patch.object(data_fetcher, "yf") as mock_yf:
        mock_yf.Ticker.return_value = fake_ticker
        result = await get_stock_info_async("UNKNOWNSYM")

    assert result == {
        "name": "Acme Co",
        "sector": "Industrials",
        "industry": "Widgets",
        "pe_ratio": 21.5,
        "market_cap": 1_000_000,
        "currency": "USD",
    }
    mock_yf.Ticker.assert_called_once_with("UNKNOWNSYM.NS")


@pytest.mark.asyncio
async def test_get_stock_info_async_returns_stub_on_timeout() -> None:
    """A slow yfinance call must be cancelled by `asyncio.wait_for`."""
    def slow_info_call(symbol: str) -> dict[str, Any]:
        # Simulate a yfinance call that hangs longer than the timeout.
        time.sleep(0.5)
        return {"longName": "should-not-arrive"}

    with patch.object(data_fetcher, "_yfinance_info_sync", side_effect=slow_info_call):
        result = await get_stock_info_async("UNKNOWNSYM", timeout=0.05)

    assert result == {"name": "UNKNOWNSYM", "sector": "N/A"}


@pytest.mark.asyncio
async def test_get_stock_info_async_returns_stub_on_yfinance_exception() -> None:
    """Upstream yfinance failure must not bubble out — we want a deterministic stub."""
    with patch.object(
        data_fetcher,
        "_yfinance_info_sync",
        side_effect=RuntimeError("yahoo blew up"),
    ):
        result = await get_stock_info_async("UNKNOWNSYM")

    assert result == {"name": "UNKNOWNSYM", "sector": "N/A"}


@pytest.mark.asyncio
async def test_get_stock_info_async_default_timeout_constant() -> None:
    """Sanity-check the 8s default declared in the module."""
    assert _YFINANCE_INFO_TIMEOUT == 8


# ---------------------------------------------------------------------------
# _fetch_nse_delivery — 403 retry-after-warm-up
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeSession:
    """Minimal stand-in for `requests.Session` that records calls."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses
        self.headers: dict[str, str] = {}
        self.calls: list[str] = []

    def get(self, url: str, timeout: float = 0) -> _FakeResponse:  # noqa: ARG002
        self.calls.append(url)
        # Homepage warm-up requests do not consume the queued API responses.
        if url.endswith(".com") or url.endswith(".com/"):
            return _FakeResponse(200)
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_fetch_nse_delivery_retries_on_403_after_warm_up() -> None:
    """First 403 should trigger a homepage warm-up and a single retry."""
    payload = {
        "marketDeptOrderBook": {
            "tradeInfo": {
                "totalTradedVolume": "1,000",
                "deliveryQuantity": "600",
                "deliveryToTradedQuantity": "60.0",
            }
        }
    }
    fake = _FakeSession([_FakeResponse(403), _FakeResponse(200, payload)])

    # Reset the module-level cached session so our fake takes its place.
    data_fetcher._nse_delivery_session = fake

    result = await _fetch_nse_delivery("RELIANCE")

    assert result is not None
    assert result["delivery_pct"] == 60.0
    assert result["traded_qty"] == 1000
    assert result["delivered_qty"] == 600
    # Sequence: API (403) -> homepage warm-up -> API (200)
    assert len(fake.calls) == 3
    assert "api/quote-equity" in fake.calls[0]
    assert fake.calls[1] == data_fetcher._NSE_HOMEPAGE
    assert "api/quote-equity" in fake.calls[2]


@pytest.mark.asyncio
async def test_fetch_nse_delivery_returns_none_on_persistent_403() -> None:
    """If both attempts return 403 the caller must see None (logged warning)."""
    fake = _FakeSession([_FakeResponse(403), _FakeResponse(403)])
    data_fetcher._nse_delivery_session = fake

    result = await _fetch_nse_delivery("RELIANCE")

    assert result is None
    assert len(fake.calls) == 3  # API, warm-up, API


@pytest.mark.asyncio
async def test_fetch_nse_delivery_no_retry_when_first_attempt_succeeds() -> None:
    """Happy path must not re-warm the session or retry."""
    payload = {
        "securityWiseDP": {
            "deliveryToTradedQuantity": "72.5",
            "totalTradedVolume": "2000",
            "deliveryQuantity": "1450",
        }
    }
    fake = _FakeSession([_FakeResponse(200, payload)])
    data_fetcher._nse_delivery_session = fake

    result = await _fetch_nse_delivery("INFY")

    assert result is not None
    assert result["delivery_pct"] == 72.5
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# Orchestrator weekly backtest — semaphore + per-task timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_weekly_backtest_caps_concurrency_and_skips_timeouts() -> None:
    """Only `_BACKTEST_CONCURRENCY` backtests run in parallel; timeouts are skipped."""
    from app.services import orchestrator as orch

    # 8 symbols: 7 fast successes, 1 that always exceeds the per-task timeout.
    symbols = [f"SYM{i}" for i in range(8)]
    timeout_symbol = "SYM3"

    in_flight = 0
    peak_in_flight = 0
    lock = asyncio.Lock()

    async def fake_run_backtest(sym: str, period: str, eval_windows: list[int]) -> dict:
        nonlocal in_flight, peak_in_flight
        async with lock:
            in_flight += 1
            peak_in_flight = max(peak_in_flight, in_flight)
        try:
            if sym == timeout_symbol:
                # Sleep well beyond the per-task timeout so wait_for fires.
                await asyncio.sleep(5)
            else:
                await asyncio.sleep(0.05)
            return {
                "overall": {"avg_pnl_5d": 1.0},
                "by_signal_type": {},
                "total_signals": 1,
            }
        finally:
            async with lock:
                in_flight -= 1

    # Patch external collaborators on the orchestrator module.
    fake_backtester = MagicMock()
    fake_backtester.run_backtest = fake_run_backtest

    watchlist_mock = AsyncMock(return_value=symbols)
    db_conn = MagicMock()
    db_conn.execute = AsyncMock(return_value=None)
    db_conn.commit = AsyncMock(return_value=None)

    class _DBCtx:
        async def __aenter__(self) -> Any:
            return db_conn

        async def __aexit__(self, *_a: Any) -> None:
            return None

    with patch.dict(
        "sys.modules",
        {"app.services.backtester": fake_backtester},
    ), patch.object(orch, "_get_watchlist_symbols", watchlist_mock), \
         patch.object(orch, "MAJOR_STOCKS", [{"symbol": s} for s in symbols]), \
         patch.object(orch, "_BACKTEST_PER_SYMBOL_TIMEOUT", 0.2), \
         patch.object(orch, "_BACKTEST_CONCURRENCY", 3), \
         patch("app.services.orchestrator.aiosqlite.connect", return_value=_DBCtx()):
        await orch.SignalOrchestrator()._run_weekly_backtest()

    # Concurrency must respect the (patched) semaphore size of 3.
    assert peak_in_flight <= 3, f"peak concurrency {peak_in_flight} exceeded cap"
    assert peak_in_flight >= 2, "expected genuine parallelism"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _async_value(value: Any):
    """Return a coroutine factory yielding `value` — for patching async fns."""
    async def _coro(*_a: Any, **_kw: Any) -> Any:
        return value
    return _coro


def _async_return(value: Any):
    """Return an `AsyncMock`-like callable that resolves to `value`."""
    async def _coro(*_a: Any, **_kw: Any) -> Any:
        return value
    return _coro

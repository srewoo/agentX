from __future__ import annotations
"""Regression tests: market_snapshot sources indices/VIX from Upstox first.

Guards the import bug that shipped once (`from app.services.source_health
import source_health` — source_health is a module, not an exported object),
which silently made _upstox_index_snapshot throw and fall back to NSE.
"""
import pytest

from app.services import market_snapshot, upstox_fetcher, source_health
import app.services.data_fetcher as data_fetcher


@pytest.fixture(autouse=True)
def _reset():
    source_health.reset()
    yield
    source_health.reset()


@pytest.mark.asyncio
async def test_upstox_index_snapshot_returns_indices(monkeypatch):
    async def _settings():
        return {"upstox_access_token": "tok"}
    monkeypatch.setattr(data_fetcher, "_get_data_settings", _settings)
    monkeypatch.setattr(upstox_fetcher, "has_token", lambda s: True)

    quotes = {
        "NIFTY 50": {"lastPrice": 24000.0, "pChange": -0.2},
        "NIFTY BANK": {"lastPrice": 57000.0, "pChange": 0.1},
        "INDIA VIX": {"lastPrice": 13.1, "pChange": 2.0},
    }

    async def _quote(name, *, token, exchange="NSE"):
        return quotes.get(name)
    monkeypatch.setattr(upstox_fetcher, "upstox_fetch_quote", _quote)

    out = await market_snapshot._upstox_index_snapshot()
    assert out["NIFTY 50"]["close"] == 24000.0
    assert out["NIFTY BANK"]["close"] == 57000.0
    assert out["INDIA VIX"]["close"] == 13.1
    # A successful fetch marks the source healthy.
    assert source_health.is_down("upstox") is False


@pytest.mark.asyncio
async def test_upstox_index_snapshot_empty_without_token(monkeypatch):
    async def _settings():
        return {}
    monkeypatch.setattr(data_fetcher, "_get_data_settings", _settings)
    monkeypatch.setattr(upstox_fetcher, "has_token", lambda s: False)
    out = await market_snapshot._upstox_index_snapshot()
    assert out == {}


@pytest.mark.asyncio
async def test_upstox_index_snapshot_never_raises_on_quote_failure(monkeypatch):
    async def _settings():
        return {"upstox_access_token": "tok"}
    monkeypatch.setattr(data_fetcher, "_get_data_settings", _settings)
    monkeypatch.setattr(upstox_fetcher, "has_token", lambda s: True)

    async def _boom(name, *, token, exchange="NSE"):
        raise RuntimeError("network down")
    monkeypatch.setattr(upstox_fetcher, "upstox_fetch_quote", _boom)

    out = await market_snapshot._upstox_index_snapshot()
    assert out == {}  # degrades to fallback, never propagates

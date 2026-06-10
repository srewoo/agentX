"""Tests for the data_fetcher source waterfall + negative cache."""
from __future__ import annotations

import pandas as pd
import pytest

from app.services import data_fetcher, source_health


def _df(n=10):
    return pd.DataFrame({
        "Open": [1.0] * n, "High": [1.0] * n, "Low": [1.0] * n,
        "Close": [1.0] * n, "Volume": [1.0] * n,
    })


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    source_health.reset()
    # Default: no token, no twelvedata key.
    async def _settings():
        return {}
    monkeypatch.setattr(data_fetcher, "_get_data_settings", _settings)
    yield
    source_health.reset()


@pytest.mark.asyncio
async def test_upstox_used_first_when_token_present(monkeypatch):
    async def _settings():
        return {"upstox_access_token": "tok"}
    monkeypatch.setattr(data_fetcher, "_get_data_settings", _settings)

    from app.services import upstox_fetcher
    monkeypatch.setattr(upstox_fetcher, "has_token", lambda s: True)

    called = {}

    async def _up(symbol, **kw):
        called["upstox"] = True
        return _df()
    monkeypatch.setattr(upstox_fetcher, "upstox_fetch_history", _up)

    async def _nse(*a, **k):
        called["nse"] = True
        return _df()
    monkeypatch.setattr(data_fetcher, "nse_fetch_history", _nse)

    out = await data_fetcher.async_fetch_history("RELIANCE", period="1y")
    assert not out.empty
    assert called.get("upstox") is True
    assert "nse" not in called  # Upstox short-circuited the waterfall


@pytest.mark.asyncio
async def test_falls_through_to_jugaad_when_nse_empty(monkeypatch):
    async def _nse(*a, **k):
        return pd.DataFrame()  # NSE 403/empty
    monkeypatch.setattr(data_fetcher, "nse_fetch_history", _nse)

    async def _jugaad(symbol, days):
        return _df()
    monkeypatch.setattr(data_fetcher, "_jugaad_fetch", _jugaad)

    out = await data_fetcher.async_fetch_history("RELIANCE", period="1y")
    assert not out.empty
    # NSE returned empty ⇒ parked in the negative cache.
    assert source_health.is_down("nse") is True


@pytest.mark.asyncio
async def test_parked_source_is_skipped(monkeypatch):
    source_health.mark_down("nse", cooldown=300)

    nse_called = {"n": 0}

    async def _nse(*a, **k):
        nse_called["n"] += 1
        return _df()
    monkeypatch.setattr(data_fetcher, "nse_fetch_history", _nse)

    async def _jugaad(symbol, days):
        return _df()
    monkeypatch.setattr(data_fetcher, "_jugaad_fetch", _jugaad)

    out = await data_fetcher.async_fetch_history("RELIANCE", period="1y")
    assert not out.empty
    assert nse_called["n"] == 0  # skipped while parked


@pytest.mark.asyncio
async def test_intraday_goes_straight_to_yfinance(monkeypatch):
    nse_called = {"n": 0}

    async def _nse(*a, **k):
        nse_called["n"] += 1
        return _df()
    monkeypatch.setattr(data_fetcher, "nse_fetch_history", _nse)

    async def _yf(symbol, period, interval, exchange):
        return _df()
    monkeypatch.setattr(data_fetcher, "_yfinance_with_cooldown", _yf)

    out = await data_fetcher.async_fetch_history("RELIANCE", period="5d", interval="5m")
    assert not out.empty
    assert nse_called["n"] == 0  # intraday never touches NSE


@pytest.mark.asyncio
async def test_quote_falls_back_to_bhavcopy_when_live_sources_down(monkeypatch):
    """When Upstox/broker/NSE/yfinance are all unavailable, get_stock_quote
    serves the cached bulk bhavcopy last-close instead of a null quote."""
    # No upstox token, no broker (empty settings from the autouse fixture).
    # Park NSE + yfinance so the waterfall skips them.
    source_health.mark_down("nse")
    source_health.mark_down("yfinance")

    from app.services import bhavcopy

    async def _eod(symbol):
        assert symbol == "RELIANCE"
        return {
            "symbol": symbol, "lastPrice": 2945.5, "previousClose": 2900.0,
            "change": 45.5, "pChange": 1.57, "open": 2910.0, "high": 2950.0,
            "low": 2905.0, "totalTradedVolume": 1234567, "source": "bhavcopy",
        }
    monkeypatch.setattr(bhavcopy, "get_eod_quote", _eod)

    q = await data_fetcher.get_stock_quote("RELIANCE")
    assert q["lastPrice"] == 2945.5
    assert q["source"] == "bhavcopy"
    assert q["symbol"] == "RELIANCE"


@pytest.mark.asyncio
async def test_delivery_falls_back_to_bhavcopy(monkeypatch):
    async def _no_nse(symbol):
        return None
    monkeypatch.setattr(data_fetcher, "_fetch_nse_delivery", _no_nse)

    from app.services import bhavcopy

    async def _deliv(symbol):
        return {"symbol": symbol, "delivery_pct": 64.8, "traded_qty": 100,
                "delivered_qty": 64, "source": "bhavcopy"}
    monkeypatch.setattr(bhavcopy, "get_delivery_pct", _deliv)

    d = await data_fetcher.get_delivery_volume("RELIANCE")
    assert d["delivery_pct"] == 64.8
    assert d["source"] == "bhavcopy"

"""Tests for the Upstox data source (instrument map, history, quote)."""
from __future__ import annotations

import gzip
import json

import pytest

from app.services import upstox_fetcher


class _FakeResp:
    def __init__(self, *, status=200, json_data=None, content=b""):
        self.status_code = status
        self._json = json_data
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


@pytest.fixture(autouse=True)
def _reset_instrument_cache():
    upstox_fetcher._instrument_maps.clear()
    upstox_fetcher._instrument_loaded_at.clear()
    yield
    upstox_fetcher._instrument_maps.clear()
    upstox_fetcher._instrument_loaded_at.clear()


def test_has_token():
    assert upstox_fetcher.has_token({"upstox_access_token": "abc"}) is True
    assert upstox_fetcher.has_token({"upstox_access_token": ""}) is False
    assert upstox_fetcher.has_token({}) is False


def test_instrument_map_parses_only_equities(monkeypatch, tmp_path):
    records = [
        {"trading_symbol": "RELIANCE", "instrument_key": "NSE_EQ|INE002A01018", "instrument_type": "EQ"},
        {"trading_symbol": "NIFTY", "instrument_key": "NSE_INDEX|Nifty 50", "instrument_type": "INDEX"},
    ]
    gz = gzip.compress(json.dumps(records).encode())
    monkeypatch.setattr(upstox_fetcher, "_CACHE_DIR", tmp_path)
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(content=gz))

    mapping = upstox_fetcher._load_instrument_map("NSE")
    assert mapping == {"RELIANCE": "NSE_EQ|INE002A01018"}


@pytest.mark.asyncio
async def test_fetch_history_parses_candles(monkeypatch):
    monkeypatch.setattr(
        upstox_fetcher, "_resolve_instrument_key", lambda s, e: "NSE_EQ|INE002A01018"
    )
    payload = {"data": {"candles": [
        ["2024-01-02T00:00:00+05:30", 100, 110, 95, 105, 1000, 0],
        ["2024-01-01T00:00:00+05:30", 98, 102, 96, 100, 800, 0],
    ]}}
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(json_data=payload))

    df = await upstox_fetcher.upstox_fetch_history(
        "RELIANCE", days=30, interval="1d", token="tok",
    )
    assert df is not None and len(df) == 2
    # Sorted ascending — oldest first.
    assert list(df["Close"]) == [100.0, 105.0]


@pytest.mark.asyncio
async def test_fetch_history_intraday_returns_none(monkeypatch):
    monkeypatch.setattr(
        upstox_fetcher, "_resolve_instrument_key", lambda s, e: "NSE_EQ|X"
    )
    df = await upstox_fetcher.upstox_fetch_history(
        "RELIANCE", days=5, interval="5m", token="tok",
    )
    assert df is None


@pytest.mark.asyncio
async def test_fetch_history_401_returns_none(monkeypatch):
    monkeypatch.setattr(upstox_fetcher, "_resolve_instrument_key", lambda s, e: "NSE_EQ|X")
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(status=401))
    df = await upstox_fetcher.upstox_fetch_history("RELIANCE", days=30, token="bad")
    assert df is None


@pytest.mark.asyncio
async def test_fetch_quote_parses_payload(monkeypatch):
    monkeypatch.setattr(upstox_fetcher, "_resolve_instrument_key", lambda s, e: "NSE_EQ|X")
    payload = {"data": {"NSE_EQ:RELIANCE": {
        "last_price": 105.0, "net_change": 5.0,
        "ohlc": {"open": 101, "high": 110, "low": 99, "close": 100},
        "volume": 123456,
    }}}
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(json_data=payload))

    q = await upstox_fetcher.upstox_fetch_quote("RELIANCE", token="tok")
    assert q["lastPrice"] == 105.0
    assert q["previousClose"] == 100
    assert q["pChange"] == 5.0  # (105-100)/100*100
    assert q["source"] == "upstox"


@pytest.mark.asyncio
async def test_fetch_quote_unresolved_symbol_returns_none(monkeypatch):
    monkeypatch.setattr(upstox_fetcher, "_resolve_instrument_key", lambda s, e: None)
    q = await upstox_fetcher.upstox_fetch_quote("WHATISTHIS", token="tok")
    assert q is None


@pytest.mark.asyncio
async def test_intraday_unsupported_interval_returns_none(monkeypatch):
    monkeypatch.setattr(upstox_fetcher, "_resolve_instrument_key", lambda s, e: "NSE_EQ|X")
    # 5m is not a native Upstox v2 intraday bucket → None (caller uses yfinance).
    df = await upstox_fetcher.upstox_fetch_intraday("RELIANCE", interval="5m", token="tok")
    assert df is None


@pytest.mark.asyncio
async def test_intraday_1minute_parses(monkeypatch):
    monkeypatch.setattr(upstox_fetcher, "_resolve_instrument_key", lambda s, e: "NSE_EQ|X")
    payload = {"data": {"candles": [
        ["2024-01-02T09:15:00+05:30", 100, 101, 99, 100.5, 500, 0],
    ]}}
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(json_data=payload))
    df = await upstox_fetcher.upstox_fetch_intraday("RELIANCE", interval="1m", token="tok")
    assert df is not None and len(df) == 1


@pytest.mark.asyncio
async def test_option_chain_normalizes_to_nse_shape(monkeypatch):
    monkeypatch.setattr(upstox_fetcher, "_resolve_underlying_key", lambda s, e: "NSE_INDEX|Nifty 50")

    contracts = {"data": [{"expiry": "2099-01-25"}, {"expiry": "2099-02-29"}]}
    chain = {"data": [{
        "strike_price": 20000, "underlying_spot_price": 19950,
        "call_options": {"market_data": {"ltp": 120, "oi": 1000, "prev_oi": 900, "volume": 50},
                         "option_greeks": {"iv": 14.2}},
        "put_options": {"market_data": {"ltp": 80, "oi": 2000, "prev_oi": 1500, "volume": 70},
                        "option_greeks": {"iv": 15.1}},
    }]}

    import requests

    def _fake_get(url, *a, **k):
        return _FakeResp(json_data=contracts if "option/contract" in url else chain)
    monkeypatch.setattr(requests, "get", _fake_get)

    recs = await upstox_fetcher.upstox_fetch_option_chain("NIFTY", token="tok")
    assert recs["underlying_value"] == 19950
    assert recs["expiry_dates"] == ["2099-01-25"]  # nearest future expiry
    ce = recs["strikes"][0]["CE"]
    assert ce["openInterest"] == 1000
    assert ce["changeinOpenInterest"] == 100  # oi - prev_oi
    assert ce["impliedVolatility"] == 14.2


@pytest.mark.asyncio
async def test_test_connection_ok_and_401(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(
        json_data={"data": {"user_name": "Rohan"}}))
    res = await upstox_fetcher.test_connection("good-tok")
    assert res["ok"] is True and "Rohan" in res["message"]

    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp(status=401))
    res = await upstox_fetcher.test_connection("bad-tok")
    assert res["ok"] is False

    res = await upstox_fetcher.test_connection("")
    assert res["ok"] is False

from __future__ import annotations
"""3.1 — persistent point-in-time price store: ingest, reproducible reads, provenance."""
import os
import tempfile

import pandas as pd
import pytest

from app.services import pit_price_store as store


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    os.unlink(path)


def _frame(adjustment="split_bonus_adjusted", source="upstox"):
    idx = pd.date_range("2024-01-01", periods=5, freq="D")
    df = pd.DataFrame({
        "Open": [100, 101, 102, 103, 104],
        "High": [101, 102, 103, 104, 105],
        "Low": [99, 100, 101, 102, 103],
        "Close": [100.5, 101.5, 102.5, 103.5, 104.5],
        "Volume": [1000, 1100, 1200, 1300, 1400],
    }, index=idx)
    df.attrs["px_adjustment"] = adjustment
    df.attrs["px_source"] = source
    return df


@pytest.mark.asyncio
async def test_ingest_then_read_is_reproducible(db, monkeypatch):
    async def _fake_fetch(symbol, **kw):
        return _frame()
    monkeypatch.setattr("app.services.data_fetcher.async_fetch_history", _fake_fetch)

    res = await store.ingest_symbol("INFY", db_path=db)
    assert res["ingested"] == 5
    assert res["adjustment"] == "split_bonus_adjusted"

    df1 = await store.get_prices("INFY", db_path=db)
    df2 = await store.get_prices("INFY", db_path=db)
    assert len(df1) == 5
    # Same query → same answer, byte for byte.
    pd.testing.assert_frame_equal(df1, df2)
    assert list(df1["Close"]) == [100.5, 101.5, 102.5, 103.5, 104.5]


@pytest.mark.asyncio
async def test_reads_do_not_refetch(db, monkeypatch):
    calls = {"n": 0}

    async def _counting_fetch(symbol, **kw):
        calls["n"] += 1
        return _frame()
    monkeypatch.setattr("app.services.data_fetcher.async_fetch_history", _counting_fetch)

    await store.ingest_symbol("INFY", db_path=db)     # 1 fetch at ingest
    for _ in range(3):
        await store.get_prices("INFY", db_path=db)    # reads hit the store only
    assert calls["n"] == 1                            # never re-fetched per read


@pytest.mark.asyncio
async def test_ingest_is_idempotent_upsert(db, monkeypatch):
    async def _fake_fetch(symbol, **kw):
        return _frame()
    monkeypatch.setattr("app.services.data_fetcher.async_fetch_history", _fake_fetch)
    await store.ingest_symbol("INFY", db_path=db)
    await store.ingest_symbol("INFY", db_path=db)     # re-ingest same dates
    cov = await store.coverage("INFY", db_path=db)
    assert cov["bars"] == 5                            # no duplicate rows


@pytest.mark.asyncio
async def test_date_range_filter(db, monkeypatch):
    async def _fake_fetch(symbol, **kw):
        return _frame()
    monkeypatch.setattr("app.services.data_fetcher.async_fetch_history", _fake_fetch)
    await store.ingest_symbol("INFY", db_path=db)
    df = await store.get_prices("INFY", start="2024-01-03", db_path=db)
    assert len(df) == 3
    assert str(df.index[0].date()) == "2024-01-03"


@pytest.mark.asyncio
async def test_get_prices_none_when_empty(db):
    assert await store.get_prices("NOPE", db_path=db) is None


@pytest.mark.asyncio
async def test_pit_first_falls_back_to_waterfall(db, monkeypatch):
    async def _fake_fetch(symbol, **kw):
        return _frame(source="yfinance")
    monkeypatch.setattr("app.services.data_fetcher.async_fetch_history", _fake_fetch)
    # Store empty → falls back to the live waterfall.
    df = await store.get_history_pit_first("TCS", db_path=db)
    assert df is not None and len(df) == 5

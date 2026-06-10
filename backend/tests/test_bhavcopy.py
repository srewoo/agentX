"""Tests for the bulk NSE bhavcopy EOD source (parser + quote/delivery shapes)."""
from __future__ import annotations

from datetime import date

import pytest

from app.services import bhavcopy

# NSE ships sec_bhavdata_full with a leading space in every header AND cell.
_SAMPLE = (
    " SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE,"
    " LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS,"
    " NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
    "RELIANCE, EQ, 09-Jun-2026, 2900.00, 2910.00, 2950.00, 2905.00, 2940.00,"
    " 2945.50, 2930.00, 1234567, 36000.00, 50000, 800000, 64.80\n"
    "IDEA, BE, 09-Jun-2026, 12.00, 12.10, 12.50, 12.00, 12.40, 12.45, 12.30,"
    " 9999, 120.00, 100, 5000, 50.00\n"
    "SOMEFUT, FUTSTK, 09-Jun-2026, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1\n"
)


@pytest.fixture(autouse=True)
def _reset_cache():
    bhavcopy._latest_map.clear()
    bhavcopy._latest_date_iso = None
    bhavcopy._latest_resolved_at = 0.0
    yield
    bhavcopy._latest_map.clear()
    bhavcopy._latest_date_iso = None
    bhavcopy._latest_resolved_at = 0.0


def test_parse_strips_whitespace_and_filters_series():
    m = bhavcopy._parse_sec_bhavdata(_SAMPLE)
    # FUTSTK is not an equity series — excluded.
    assert set(m.keys()) == {"RELIANCE", "IDEA"}
    r = m["RELIANCE"]
    assert r["close"] == 2945.50
    assert r["open"] == 2910.00
    assert r["prev_close"] == 2900.00
    assert r["volume"] == 1234567
    assert r["delivery_qty"] == 800000
    assert r["delivery_pct"] == 64.80


def test_parse_handles_blank_and_dash_cells():
    csv = (
        " SYMBOL, SERIES, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE,"
        " LAST_PRICE, CLOSE_PRICE, TTL_TRD_QNTY, DELIV_QTY, DELIV_PER\n"
        "ZEROVOL, EQ, 10.00, -, -, -, 10.00, 10.00, 0, , \n"
    )
    m = bhavcopy._parse_sec_bhavdata(csv)
    row = m["ZEROVOL"]
    assert row["open"] is None
    assert row["delivery_pct"] is None
    assert row["volume"] == 0


def test_parse_skips_rows_without_close():
    csv = (
        " SYMBOL, SERIES, CLOSE_PRICE\n"
        "NOCLOSE, EQ, \n"
        "OK, EQ, 5.0\n"
    )
    m = bhavcopy._parse_sec_bhavdata(csv)
    assert "NOCLOSE" not in m
    assert m["OK"]["close"] == 5.0


@pytest.mark.asyncio
async def test_get_eod_quote_shape(monkeypatch):
    async def _bc(trade_date=None):
        return bhavcopy._parse_sec_bhavdata(_SAMPLE)
    monkeypatch.setattr(bhavcopy, "get_bhavcopy", _bc)

    q = await bhavcopy.get_eod_quote("RELIANCE.NS")
    assert q["symbol"] == "RELIANCE.NS"
    assert q["lastPrice"] == 2945.50
    assert q["previousClose"] == 2900.00
    assert q["change"] == 45.50
    assert q["source"] == "bhavcopy"

    assert await bhavcopy.get_eod_quote("NOTLISTED") is None


@pytest.mark.asyncio
async def test_get_delivery_pct_shape(monkeypatch):
    async def _bc(trade_date=None):
        return bhavcopy._parse_sec_bhavdata(_SAMPLE)
    monkeypatch.setattr(bhavcopy, "get_bhavcopy", _bc)

    d = await bhavcopy.get_delivery_pct("RELIANCE")
    assert d["delivery_pct"] == 64.80
    assert d["delivered_qty"] == 800000
    assert d["traded_qty"] == 1234567
    assert d["source"] == "bhavcopy"


def test_latest_resolution_walks_back_over_weekend(monkeypatch):
    # Sunday 2026-06-07 → must skip Sat/Sun and land on Fri 2026-06-05.
    tried: list[date] = []

    def _load(day):
        tried.append(day)
        if day == date(2026, 6, 5):
            return {"RELIANCE": {"close": 1.0}}
        return None

    monkeypatch.setattr(bhavcopy, "_load_day", _load)
    iso = bhavcopy._sync_get_latest(date(2026, 6, 7))
    assert iso == "2026-06-05"
    # Weekends are skipped without a network/disk attempt.
    assert date(2026, 6, 6) not in tried  # Sat
    assert date(2026, 6, 7) not in tried  # Sun


def test_latest_resolution_gives_up(monkeypatch):
    monkeypatch.setattr(bhavcopy, "_load_day", lambda d: None)
    assert bhavcopy._sync_get_latest(date(2026, 6, 9), max_lookback=3) is None

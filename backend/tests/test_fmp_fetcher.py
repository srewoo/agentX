from __future__ import annotations
"""Tests for app.services.fmp_fetcher — earnings calendar / blackout."""
from datetime import datetime, timezone

import pytest

from app.services import fmp_fetcher, source_health


@pytest.fixture(autouse=True)
def _clean():
    source_health.reset()
    fmp_fetcher._reset_cache()
    yield
    source_health.reset()
    fmp_fetcher._reset_cache()


class TestPure:
    def test_norm_strips_exchange_suffix(self):
        assert fmp_fetcher._norm("RELIANCE.NS") == "RELIANCE"
        assert fmp_fetcher._norm("tcs.bo") == "TCS"
        assert fmp_fetcher._norm("INFY") == "INFY"

    def test_is_blackout_matches_across_suffixes(self):
        rows = [{"symbol": "RELIANCE.NS", "date": "2026-06-03"}]
        assert fmp_fetcher.is_blackout(rows, "RELIANCE") is True
        assert fmp_fetcher.is_blackout(rows, "RELIANCE.BO") is True
        assert fmp_fetcher.is_blackout(rows, "TCS") is False

    def test_is_blackout_empty(self):
        assert fmp_fetcher.is_blackout([], "RELIANCE") is False


@pytest.mark.asyncio
async def test_blackout_none_without_key(monkeypatch):
    async def _no_key():
        return None
    monkeypatch.setattr(fmp_fetcher, "_get_api_key", _no_key)
    assert await fmp_fetcher.is_in_earnings_blackout("RELIANCE") is None


@pytest.mark.asyncio
async def test_blackout_true_when_symbol_has_results(monkeypatch):
    async def _key():
        return "demo"
    monkeypatch.setattr(fmp_fetcher, "_get_api_key", _key)
    monkeypatch.setattr(
        fmp_fetcher, "_fetch_earnings_sync",
        lambda frm, to, key: [{"symbol": "RELIANCE.NS", "date": to}],
    )
    now = datetime(2026, 6, 2, tzinfo=timezone.utc)
    assert await fmp_fetcher.is_in_earnings_blackout("RELIANCE", now=now) is True
    assert await fmp_fetcher.is_in_earnings_blackout("TCS", now=now) is False


@pytest.mark.asyncio
async def test_failed_fetch_marks_source_down_and_returns_none(monkeypatch):
    async def _key():
        return "demo"
    monkeypatch.setattr(fmp_fetcher, "_get_api_key", _key)
    monkeypatch.setattr(fmp_fetcher, "_fetch_earnings_sync", lambda frm, to, key: None)
    assert await fmp_fetcher.is_in_earnings_blackout("RELIANCE") is None
    assert source_health.is_down("fmp") is True


@pytest.mark.asyncio
async def test_calendar_is_cached(monkeypatch):
    calls = {"n": 0}

    def _fetch(frm, to, key):
        calls["n"] += 1
        return [{"symbol": "INFY.NS", "date": to}]

    async def _key():
        return "demo"
    monkeypatch.setattr(fmp_fetcher, "_get_api_key", _key)
    monkeypatch.setattr(fmp_fetcher, "_fetch_earnings_sync", _fetch)
    now = datetime(2026, 6, 2, tzinfo=timezone.utc)
    await fmp_fetcher.is_in_earnings_blackout("INFY", now=now)
    await fmp_fetcher.is_in_earnings_blackout("INFY", now=now)
    assert calls["n"] == 1  # second call served from cache

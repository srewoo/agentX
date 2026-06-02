from __future__ import annotations
"""Tests for app.services.finnhub_fetcher — macro / USD-INR."""
import pytest

from app.services import finnhub_fetcher, source_health


@pytest.fixture(autouse=True)
def _clean():
    source_health.reset()
    yield
    source_health.reset()


class TestParse:
    def test_parses_inr_rate(self):
        data = {"base": "USD", "quote": {"INR": 83.21, "EUR": 0.92}}
        assert finnhub_fetcher.parse_forex_rate(data, "INR") == pytest.approx(83.21)

    def test_missing_quote_returns_none(self):
        assert finnhub_fetcher.parse_forex_rate({"base": "USD"}, "INR") is None
        assert finnhub_fetcher.parse_forex_rate({}, "INR") is None
        assert finnhub_fetcher.parse_forex_rate(None, "INR") is None

    def test_non_positive_or_bad_value_returns_none(self):
        assert finnhub_fetcher.parse_forex_rate({"quote": {"INR": 0}}, "INR") is None
        assert finnhub_fetcher.parse_forex_rate({"quote": {"INR": "x"}}, "INR") is None


@pytest.mark.asyncio
async def test_usd_inr_none_without_key(monkeypatch):
    async def _no_key():
        return None
    monkeypatch.setattr(finnhub_fetcher, "_get_api_key", _no_key)
    assert await finnhub_fetcher.get_usd_inr() is None


@pytest.mark.asyncio
async def test_usd_inr_returns_rate(monkeypatch):
    async def _key():
        return "demo"
    monkeypatch.setattr(finnhub_fetcher, "_get_api_key", _key)
    monkeypatch.setattr(
        finnhub_fetcher, "_fetch_forex_sync",
        lambda key, base="USD": {"base": "USD", "quote": {"INR": 83.5}},
    )
    assert await finnhub_fetcher.get_usd_inr() == pytest.approx(83.5)


@pytest.mark.asyncio
async def test_failed_fetch_marks_down(monkeypatch):
    async def _key():
        return "demo"
    monkeypatch.setattr(finnhub_fetcher, "_get_api_key", _key)
    monkeypatch.setattr(finnhub_fetcher, "_fetch_forex_sync", lambda key, base="USD": None)
    assert await finnhub_fetcher.get_usd_inr() is None
    assert source_health.is_down("finnhub") is True

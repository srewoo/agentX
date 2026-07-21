"""Tests for the canonical price-adjustment layer."""
from __future__ import annotations

import pandas as pd
import pytest

from app.services import price_adjuster


def _raw_frame_with_split():
    """6 bars; a 2:1 split takes effect on bar index 3 (2024-01-04).

    Raw (unadjusted) prices: 100 before the split, 50 after — the classic
    halving discontinuity that corrupts technicals if left unadjusted.
    """
    idx = pd.date_range("2024-01-01", periods=6, freq="D")
    return pd.DataFrame({
        "Open":   [100.0, 100.0, 100.0, 50.0, 50.0, 50.0],
        "High":   [101.0, 101.0, 101.0, 50.5, 50.5, 50.5],
        "Low":    [99.0,  99.0,  99.0,  49.5, 49.5, 49.5],
        "Close":  [100.0, 100.0, 100.0, 50.0, 50.0, 50.0],
        "Volume": [1000.0, 1000.0, 1000.0, 2000.0, 2000.0, 2000.0],
    }, index=idx)


class TestApplySplitEvents:
    def test_should_back_adjust_prices_before_split_when_event_given(self):
        df = _raw_frame_with_split()
        out = price_adjuster.apply_split_events(
            df, [(pd.Timestamp("2024-01-04"), 2.0)]
        )
        # Pre-split bars halved onto the post-split basis; series continuous.
        assert out["Close"].tolist() == [50.0, 50.0, 50.0, 50.0, 50.0, 50.0]
        assert out["Open"].iloc[0] == 50.0
        # Volume doubled pre-split.
        assert out["Volume"].tolist() == [2000.0] * 6

    def test_should_not_modify_bars_on_or_after_split_day(self):
        df = _raw_frame_with_split()
        out = price_adjuster.apply_split_events(
            df, [(pd.Timestamp("2024-01-04"), 2.0)]
        )
        pd.testing.assert_frame_equal(out.iloc[3:], df.iloc[3:])

    def test_should_return_frame_unchanged_when_no_events(self):
        df = _raw_frame_with_split()
        out = price_adjuster.apply_split_events(df, [])
        pd.testing.assert_frame_equal(out, df)

    def test_should_ignore_split_outside_window(self):
        df = _raw_frame_with_split()
        out = price_adjuster.apply_split_events(
            df, [(pd.Timestamp("2030-01-01"), 5.0), (pd.Timestamp("2010-01-01"), 5.0)]
        )
        pd.testing.assert_frame_equal(out, df)

    def test_should_compose_multiple_splits(self):
        idx = pd.date_range("2024-01-01", periods=4, freq="D")
        df = pd.DataFrame({
            "Close": [400.0, 200.0, 200.0, 100.0],
            "Volume": [1.0, 2.0, 2.0, 4.0],
        }, index=idx)
        out = price_adjuster.apply_split_events(df, [
            (pd.Timestamp("2024-01-02"), 2.0),
            (pd.Timestamp("2024-01-04"), 2.0),
        ])
        assert out["Close"].tolist() == [100.0] * 4
        assert out["Volume"].tolist() == [4.0] * 4


class TestAdjustYfinanceFrame:
    def test_should_split_adjust_and_drop_action_columns(self):
        df = _raw_frame_with_split()
        df["Dividends"] = [0.0, 0.0, 5.0, 0.0, 0.0, 0.0]
        df["Stock Splits"] = [0.0, 0.0, 0.0, 2.0, 0.0, 0.0]
        out = price_adjuster.adjust_yfinance_frame(df)
        assert out["Close"].tolist() == [50.0] * 6
        assert "Dividends" not in out.columns
        assert "Stock Splits" not in out.columns
        assert out.attrs["px_source"] == "yfinance"
        assert out.attrs["px_adjustment"] == price_adjuster.POLICY

    def test_should_not_apply_dividend_adjustment(self):
        # Dividends present, no splits: prices must be untouched (POLICY).
        df = _raw_frame_with_split().iloc[:3]
        df["Dividends"] = [0.0, 10.0, 0.0]
        df["Stock Splits"] = [0.0, 0.0, 0.0]
        out = price_adjuster.adjust_yfinance_frame(df)
        assert out["Close"].tolist() == [100.0, 100.0, 100.0]


class TestNormalizeRaw:
    @pytest.mark.asyncio
    async def test_should_adjust_when_events_resolve(self, monkeypatch):
        async def _events(symbol):
            return [(pd.Timestamp("2024-01-04"), 2.0)]
        monkeypatch.setattr(price_adjuster, "get_split_events", _events)

        out = await price_adjuster.normalize_raw(_raw_frame_with_split(), "TCS", "nse")
        assert out["Close"].tolist() == [50.0] * 6
        assert out.attrs["px_adjustment"] == price_adjuster.POLICY
        assert out.attrs["px_source"] == "nse"

    @pytest.mark.asyncio
    async def test_should_fail_open_and_tag_unknown_when_lookup_fails(self, monkeypatch):
        async def _events(symbol):
            return None  # lookup failed
        monkeypatch.setattr(price_adjuster, "get_split_events", _events)

        df = _raw_frame_with_split()
        out = await price_adjuster.normalize_raw(df, "TCS", "nse")
        pd.testing.assert_frame_equal(out, df)
        assert out.attrs["px_adjustment"] == "unknown"


class TestSplitEventCache:
    @pytest.mark.asyncio
    async def test_should_cache_positive_result(self, monkeypatch):
        price_adjuster.reset_split_cache()
        calls = {"n": 0}

        def _fetch(symbol):
            calls["n"] += 1
            return [(pd.Timestamp("2024-01-04"), 2.0)]
        monkeypatch.setattr(price_adjuster, "_fetch_split_events_sync", _fetch)

        a = await price_adjuster.get_split_events("INFY")
        b = await price_adjuster.get_split_events("INFY")
        assert a == b
        assert calls["n"] == 1
        price_adjuster.reset_split_cache()

"""Tests for portfolio_correlation."""
from __future__ import annotations

import math
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from app.services.portfolio_correlation import (
    concentration_summary,
    correlation_to_open,
    pearson,
)


def test_pearson_known_value():
    """Perfect linear correlation should be 1.0."""
    a = [1.0, 2.0, 3.0, 4.0, 5.0]
    b = [2.0, 4.0, 6.0, 8.0, 10.0]
    assert pearson(a, b) == pytest.approx(1.0, abs=1e-9)


def test_pearson_perfect_anticorrelation():
    a = [1.0, 2.0, 3.0, 4.0, 5.0]
    b = [5.0, 4.0, 3.0, 2.0, 1.0]
    assert pearson(a, b) == pytest.approx(-1.0, abs=1e-9)


def test_pearson_returns_none_for_short_series():
    assert pearson([1, 2], [3, 4]) is None


def test_pearson_returns_none_for_zero_variance():
    assert pearson([1, 1, 1, 1, 1], [1, 2, 3, 4, 5]) is None


def _mock_df(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"Close": closes})


@pytest.mark.asyncio
async def test_correlation_to_open_empty_book_is_zero():
    assert await correlation_to_open("RELIANCE", []) == 0.0


@pytest.mark.asyncio
async def test_correlation_to_open_finds_max_correlation():
    """RELIANCE perfectly correlated to ONGC (1.0), uncorrelated to IT."""
    # Construct three series:
    # candidate "RELIANCE" mirrors ONGC exactly → corr ≈ +1.0
    # IT moves opposite, anti-correlated → |corr| ≈ 1.0 too
    closes_reliance = [100 + i for i in range(30)]
    closes_ongc = [50 + i * 0.5 for i in range(30)]    # same direction
    closes_it = [200 - i for i in range(30)]            # opposite

    series_map = {
        "RELIANCE": closes_reliance,
        "ONGC": closes_ongc,
        "INFY": closes_it,
    }

    async def fake_history(symbol, period="3mo", interval="1d"):
        return _mock_df(series_map[symbol])

    with patch("app.services.data_fetcher.async_fetch_history", new=AsyncMock(side_effect=fake_history)):
        c = await correlation_to_open(
            "RELIANCE",
            [{"symbol": "ONGC"}, {"symbol": "INFY"}],
        )
    # Returns max abs corr — both legs are |1.0| in this construction.
    assert c == pytest.approx(1.0, abs=0.01)


@pytest.mark.asyncio
async def test_correlation_skips_already_open_symbol():
    """If candidate IS in the book, return 0 (handled by dedup elsewhere)."""
    c = await correlation_to_open("RELIANCE", [{"symbol": "RELIANCE"}])
    assert c == 0.0


# ── concentration_summary (pure compute) ──────────────────────────────────


def test_concentration_summary_aggregates_by_sector():
    positions = [
        {"symbol": "RELIANCE", "sector": "Energy", "entry_price": 2400, "shares": 10},
        {"symbol": "ONGC", "sector": "Energy", "entry_price": 300, "shares": 100},
        {"symbol": "INFY", "sector": "IT", "entry_price": 1500, "shares": 5},
    ]
    out = concentration_summary(positions, capital=1_000_000)
    # Energy: (24000 + 30000) / 1_000_000 = 5.4%
    # IT: 7500 / 1_000_000 = 0.75%
    assert out["sector_pct"]["Energy"] == pytest.approx(5.4, abs=0.01)
    assert out["sector_pct"]["IT"] == pytest.approx(0.75, abs=0.01)
    assert out["n_positions"] == 3


def test_concentration_summary_flags_oversized_sector():
    # 30% in Energy → flagged
    positions = [
        {"symbol": "X", "sector": "Energy", "entry_price": 1000, "shares": 300},
    ]
    out = concentration_summary(positions, capital=1_000_000)
    assert any("Energy" in w and "25%" in w for w in out["warnings"])


def test_concentration_summary_flags_oversized_position():
    # One position = 8% → flagged
    positions = [
        {"symbol": "X", "sector": "IT", "entry_price": 1000, "shares": 80},
    ]
    out = concentration_summary(positions, capital=1_000_000)
    assert any("largest position" in w for w in out["warnings"])


def test_concentration_summary_handles_bad_rows():
    positions = [
        {"symbol": "X", "entry_price": "not-a-number", "shares": 10},
        {"symbol": "Y", "entry_price": 0, "shares": 10},
        {"symbol": "Z", "sector": "IT", "entry_price": 1000, "shares": 5},
    ]
    out = concentration_summary(positions, capital=1_000_000)
    # Only the valid row counts.
    assert out["n_positions"] == 1

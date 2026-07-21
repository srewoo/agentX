from __future__ import annotations
"""1.1 — widened liquid universe, ADV liquidity floor, exposure-preserving cap."""
import pandas as pd
import pytest

from app.services import liquid_universe as lu
from app.services.kelly_sizing import per_position_cap_pct, DEFAULT_MAX_POSITION_PCT


# ── universe ──
def test_universe_has_200_plus_unique_liquid_names():
    syms = lu.liquid_symbols()
    assert len(syms) >= 200
    assert len(syms) == len(set(syms))          # no duplicates
    assert "RELIANCE" in syms                    # majors preserved
    assert "DIXON" in syms                       # extension present
    assert not any(s.startswith("^") for s in syms)  # no index tickers


def test_every_universe_entry_has_a_sector():
    assert all(e.get("sector") for e in lu.LIQUID_UNIVERSE)


# ── ADV liquidity floor ──
def _ohlcv(close: float, volume: float, n: int = 30) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame({"Close": [close] * n, "Volume": [volume] * n}, index=idx)


def test_compute_adv_cr():
    # 100 × 1,000,000 = 1e8 = ₹10cr ADV.
    assert lu.compute_adv_cr(_ohlcv(100, 1_000_000)) == pytest.approx(10.0, abs=1e-6)
    # 50 × 200,000 = 1e7 = ₹1cr.
    assert lu.compute_adv_cr(_ohlcv(50, 200_000)) == pytest.approx(1.0, abs=1e-6)


def test_compute_adv_cr_missing_columns_returns_none():
    assert lu.compute_adv_cr(pd.DataFrame({"Close": [1, 2]})) is None
    assert lu.compute_adv_cr(None) is None
    assert lu.compute_adv_cr(_ohlcv(100, 0)) is None      # all-zero volume


def test_liquidity_floor():
    assert lu.passes_liquidity_floor(10.0) is True
    assert lu.passes_liquidity_floor(5.0) is True         # boundary inclusive
    assert lu.passes_liquidity_floor(4.99) is False
    assert lu.passes_liquidity_floor(None) is False       # unknown fails closed


# ── exposure-preserving per-trade cap ──
def test_per_position_cap_preserves_gross_exposure():
    # Old book of 12 → unchanged 5% cap.
    assert per_position_cap_pct(12) == pytest.approx(DEFAULT_MAX_POSITION_PCT)
    # Widened book of 30 → 2% each, so 30 × 2% = 60% gross (bounded).
    assert per_position_cap_pct(30) == pytest.approx(2.0)
    # Never exceeds the hard cap even for a tiny book.
    assert per_position_cap_pct(1) == pytest.approx(DEFAULT_MAX_POSITION_PCT)
    assert per_position_cap_pct(0) == pytest.approx(DEFAULT_MAX_POSITION_PCT)


def test_gross_exposure_bounded_across_book_sizes():
    for max_open in (12, 20, 30, 40):
        gross = per_position_cap_pct(max_open) * max_open
        assert gross <= 60.0 + 1e-9

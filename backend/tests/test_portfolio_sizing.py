from __future__ import annotations
"""B2–B5 — portfolio-aware sizing guardrails (pure functions)."""
from app.services.portfolio_sizing import (
    apply_exposure_caps,
    correlation_size_multiplier,
    drawdown_breaker_tripped,
    dynamic_kelly_fraction,
)


# B2
def test_correlation_multiplier_full_below_start():
    assert correlation_size_multiplier(0.2) == 1.0


def test_correlation_multiplier_shrinks_with_correlation():
    m_mid = correlation_size_multiplier(0.7)
    m_high = correlation_size_multiplier(0.95)
    assert 0.3 <= m_high < m_mid < 1.0


def test_correlation_multiplier_floored():
    assert abs(correlation_size_multiplier(1.0) - 0.3) < 1e-9


# B3
def test_sector_cap_trims_value():
    # 25% sector cap on 100k = 25k; 20k already open → only 5k room.
    out = apply_exposure_caps(
        10_000, "bullish", capital=100_000, sector="Banks",
        sector_value_open=20_000, gross_open=20_000, net_open=20_000)
    assert out["allowed_value"] == 5_000
    assert out["binding"] == "sector_cap"
    assert out["capped"] is True


def test_gross_cap_blocks_when_full():
    out = apply_exposure_caps(
        10_000, "bullish", capital=100_000, sector="IT",
        sector_value_open=0, gross_open=150_000, net_open=0)
    assert out["allowed_value"] == 0.0


def test_net_cap_binds_directionally():
    # Already +100k net long (= cap). A new long has zero net room...
    long_out = apply_exposure_caps(
        10_000, "bullish", capital=100_000, sector="IT",
        sector_value_open=0, gross_open=100_000, net_open=100_000)
    assert long_out["allowed_value"] == 0.0
    # ...but a short reduces |net|, so it's allowed.
    short_out = apply_exposure_caps(
        10_000, "bearish", capital=100_000, sector="IT",
        sector_value_open=0, gross_open=100_000, net_open=100_000)
    assert short_out["allowed_value"] == 10_000


# B4
def test_dynamic_fraction_never_inflates():
    assert dynamic_kelly_fraction(0.25, vix=12, recent_losses=0) == 0.25


def test_dynamic_fraction_high_vix_halves():
    assert dynamic_kelly_fraction(0.25, vix=22) == 0.125


def test_dynamic_fraction_extreme_vix_and_streak_compound():
    # extreme vix (×0.25) then losing streak (×0.5) → ×0.125
    f = dynamic_kelly_fraction(0.25, vix=30, recent_losses=4)
    assert abs(f - 0.25 * 0.125) < 1e-9


# B5
def test_drawdown_breaker_trips_past_floor():
    out = drawdown_breaker_tripped(100_000, 80_000, max_drawdown_pct=15)
    assert out["tripped"] is True
    assert out["drawdown_pct"] == 20.0


def test_drawdown_breaker_inactive_within_floor():
    out = drawdown_breaker_tripped(100_000, 95_000, max_drawdown_pct=15)
    assert out["tripped"] is False

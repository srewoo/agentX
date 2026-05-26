"""Tests for the Black-Scholes Greeks + HV fallback utilities."""
from __future__ import annotations

import math

import pytest

from app.services.options_greeks import (
    Greeks,
    compute_greeks,
    historical_volatility,
    resolve_iv,
    time_to_expiry_years,
)


# Reference values cross-checked with standard option-pricing tables for
# S=100, K=100, T=0.25 (3 months), r=0.05, sigma=0.20.
S, K, T, R, SIG = 100.0, 100.0, 0.25, 0.05, 0.20


def test_atm_call_matches_reference():
    """S=K=100, T=0.25, r=0.05, σ=0.20 → d1≈0.175, d2≈0.075. Expected price ≈ 4.63, delta ≈ 0.5695."""
    g = compute_greeks(S, K, T, SIG, r=R, option_type="call")
    assert g.price == pytest.approx(4.6147, abs=0.05)
    assert g.delta == pytest.approx(0.5695, abs=0.005)


def test_atm_put_matches_reference():
    g = compute_greeks(S, K, T, SIG, r=R, option_type="put")
    # ATM put price from put-call parity = call - (S - K·e^(-rT))
    # = 4.6147 - (100 - 100·exp(-0.0125)) = 4.6147 - 1.2422 = 3.3725
    # ATM put delta = call delta - 1 = -0.4305
    assert g.price == pytest.approx(3.3725, abs=0.05)
    assert g.delta == pytest.approx(-0.4305, abs=0.005)


def test_put_call_parity():
    """C − P = S − K·e^(-rT) — fundamental no-arbitrage relation."""
    c = compute_greeks(S, K, T, SIG, r=R, option_type="call").price
    p = compute_greeks(S, K, T, SIG, r=R, option_type="put").price
    parity_rhs = S - K * math.exp(-R * T)
    assert (c - p) == pytest.approx(parity_rhs, abs=1e-6)


def test_zero_inputs_return_zeroed_greeks():
    g = compute_greeks(0.0, K, T, SIG)
    assert isinstance(g, Greeks)
    assert g.price == 0 and g.delta == 0


def test_theta_is_negative_for_long_options():
    """Both calls and puts decay; theta should be ≤ 0."""
    call = compute_greeks(S, K, T, SIG, r=R, option_type="call")
    put = compute_greeks(S, K, T, SIG, r=R, option_type="put")
    assert call.theta < 0
    assert put.theta < 0


def test_gamma_peaks_at_the_money():
    atm = compute_greeks(S, K, T, SIG).gamma
    deep_itm = compute_greeks(S * 1.3, K, T, SIG).gamma
    deep_otm = compute_greeks(S * 0.7, K, T, SIG).gamma
    assert atm > deep_itm
    assert atm > deep_otm


def test_historical_volatility_too_short_returns_none():
    assert historical_volatility([100, 101, 102], window=30) is None


def test_historical_volatility_constant_series_is_zero():
    closes = [100.0] * 40
    hv = historical_volatility(closes, window=30)
    assert hv == 0.0


def test_historical_volatility_reasonable_range():
    # Synthetic 1% daily moves → annualised vol ~ 1% × √252 ≈ 15.9%
    import random
    rng = random.Random(42)
    closes = [100.0]
    for _ in range(60):
        closes.append(closes[-1] * (1 + rng.gauss(0, 0.01)))
    hv = historical_volatility(closes, window=30)
    assert hv is not None
    assert 0.05 < hv < 0.30


def test_resolve_iv_prefers_explicit_iv():
    assert resolve_iv(0.25, closes=[100] * 40) == 0.25


def test_resolve_iv_falls_back_to_hv_on_none():
    closes = [100 + i * 0.1 for i in range(40)]
    iv = resolve_iv(None, closes=closes)
    assert iv is not None and iv > 0


def test_resolve_iv_returns_none_when_no_data():
    assert resolve_iv(None, closes=None) is None
    assert resolve_iv(0, closes=None) is None  # 0 IV treated as missing


def test_time_to_expiry_floors_above_zero():
    assert time_to_expiry_years(0) > 0
    assert time_to_expiry_years(7) == pytest.approx(7 / 365.0)

"""Black-Scholes Greeks and volatility utilities.

Closed-form (no scipy required — uses ``math.erf`` for the normal CDF) so
this module stays dependency-free and import-safe in test environments.

Inputs are in conventional Indian equity option terms:
- ``S``: spot price (₹)
- ``K``: strike price (₹)
- ``T``: time to expiry in *years* (e.g. 7 days = 7/365)
- ``r``: annual risk-free rate as decimal (default 7% — RBI repo proxy)
- ``sigma``: implied volatility as decimal (e.g. 0.25 for 25%)
- ``option_type``: ``"call"`` or ``"put"``

Outputs are signed conventionally (call delta ∈ (0, 1), put delta ∈ (-1, 0)).

The IV-with-HV fallback solves the recurring "yfinance returned None for
IV" problem: substitute realised volatility (rolling stdev of log-returns
× √252) so downstream factor models never receive ``None`` and silently
default to zero.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Optional, Sequence

OptionType = Literal["call", "put"]

# Defaults tuned for NSE/BSE
DEFAULT_RISK_FREE_RATE = 0.07          # ~RBI repo
DEFAULT_TRADING_DAYS_PER_YEAR = 252


def _norm_cdf(x: float) -> float:
    """Cumulative normal CDF using ``math.erf``."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


@dataclass(frozen=True)
class Greeks:
    """Per-option Black-Scholes Greeks. All values are in option-pricing
    conventions; theta is *daily* (annualised θ / 365) so callers can
    surface "₹/day of decay" directly."""
    price: float
    delta: float
    gamma: float
    vega: float
    theta: float   # per calendar day
    rho: float

    def as_dict(self) -> dict:
        return {
            "price": round(self.price, 4),
            "delta": round(self.delta, 4),
            "gamma": round(self.gamma, 6),
            "vega": round(self.vega, 4),
            "theta": round(self.theta, 4),
            "rho": round(self.rho, 4),
        }


def _validate_inputs(S: float, K: float, T: float, sigma: float) -> Optional[Greeks]:
    """Reject pathological inputs early. Returns a zeroed Greeks dataclass
    so callers can keep going without branching everywhere."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return Greeks(price=0.0, delta=0.0, gamma=0.0, vega=0.0, theta=0.0, rho=0.0)
    return None


def compute_greeks(
    S: float,
    K: float,
    T: float,
    sigma: float,
    *,
    r: float = DEFAULT_RISK_FREE_RATE,
    option_type: OptionType = "call",
) -> Greeks:
    """Closed-form Black-Scholes price + Greeks for a European option.

    For Indian index options (NIFTY, BANKNIFTY) and stock options on NSE
    this is a good first-order approximation. American early-exercise
    premium is small for the typical short-dated ATM contracts the engine
    looks at, so we deliberately don't import a heavier model.
    """
    bad = _validate_inputs(S, K, T, sigma)
    if bad is not None:
        return bad

    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    pdf_d1 = _norm_pdf(d1)
    discount = math.exp(-r * T)

    if option_type == "call":
        price = S * _norm_cdf(d1) - K * discount * _norm_cdf(d2)
        delta = _norm_cdf(d1)
        # Theta in ₹/year, then divide by 365 to give daily decay (calendar).
        theta_annual = (
            -(S * pdf_d1 * sigma) / (2.0 * sqrt_T)
            - r * K * discount * _norm_cdf(d2)
        )
        rho = K * T * discount * _norm_cdf(d2) / 100.0
    else:
        price = K * discount * _norm_cdf(-d2) - S * _norm_cdf(-d1)
        delta = _norm_cdf(d1) - 1.0
        theta_annual = (
            -(S * pdf_d1 * sigma) / (2.0 * sqrt_T)
            + r * K * discount * _norm_cdf(-d2)
        )
        rho = -K * T * discount * _norm_cdf(-d2) / 100.0

    gamma = pdf_d1 / (S * sigma * sqrt_T)
    vega = S * pdf_d1 * sqrt_T / 100.0      # per 1% IV move
    theta = theta_annual / 365.0            # per calendar day

    return Greeks(price=price, delta=delta, gamma=gamma, vega=vega, theta=theta, rho=rho)


# ─────────────────────────────────────────────────────────────────────────
# Volatility utilities
# ─────────────────────────────────────────────────────────────────────────

def historical_volatility(
    closes: Sequence[float],
    *,
    window: int = 30,
    trading_days: int = DEFAULT_TRADING_DAYS_PER_YEAR,
) -> Optional[float]:
    """Annualised realised volatility from a series of close prices.

    Returns ``None`` if the series is too short. Uses log-returns
    (standard for vol calcs) and √trading_days annualisation.
    """
    if len(closes) < window + 1:
        return None
    tail = list(closes[-(window + 1):])
    log_returns: list[float] = []
    for i in range(1, len(tail)):
        prev, curr = tail[i - 1], tail[i]
        if prev <= 0 or curr <= 0:
            continue
        log_returns.append(math.log(curr / prev))
    if len(log_returns) < 2:
        return None
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    daily_std = math.sqrt(variance)
    return daily_std * math.sqrt(trading_days)


def resolve_iv(
    iv: Optional[float],
    *,
    closes: Optional[Sequence[float]] = None,
    hv_window: int = 30,
) -> Optional[float]:
    """Return the supplied IV if usable; otherwise fall back to HV.

    Resolves the "yfinance returned None / 0 for IV" gap that breaks
    downstream Greeks. Caller passes the close-price series so we can
    compute HV inline.
    """
    if iv is not None and iv > 0:
        return iv
    if not closes:
        return None
    return historical_volatility(closes, window=hv_window)


# ─────────────────────────────────────────────────────────────────────────
# Time-to-expiry helper
# ─────────────────────────────────────────────────────────────────────────

def time_to_expiry_years(days_to_expiry: float) -> float:
    """Convert days-to-expiry into the fractional year ``T`` Black-Scholes
    expects. Floors at 1 hour so an at-expiry contract still produces
    finite Greeks (callers shouldn't pass T=0)."""
    one_hour_in_years = 1.0 / (365.0 * 24.0)
    return max(one_hour_in_years, float(days_to_expiry) / 365.0)

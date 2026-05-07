"""
Sector-median fundamentals for the Indian equity market.

Hand-curated from publicly available aggregate data (Screener.in / Tijori sector
pages, NIFTY sectoral index constituents) as of 2025-Q4. These are *medians*
across the most liquid 10–30 names in each sector, not means — they're robust
to outliers (e.g. one cyclical loss-maker doesn't pollute Auto's PE).

Why hardcoded vs. computed live?
  - Computing live needs N yfinance calls per peer per sector — 30+ calls,
    rate-limit risk, slow first-paint.
  - Sector medians don't move quickly. Refresh quarterly is plenty.
  - Determinism beats marginal accuracy here — we just want a reference line.

Each entry returns the same shape as `_extract_fundamentals` so frontend can
diff field-by-field. Only the fields that meaningfully vary by sector are
populated; rest left as None to avoid spurious comparisons.
"""

from __future__ import annotations
from typing import Optional

# pe = trailing PE, pb = price/book, roe = decimal (0.18 = 18%), de = debt/equity %
# margin = profit margin (decimal), div_yield = decimal
SECTOR_MEDIANS: dict[str, dict[str, Optional[float]]] = {
    "IT": {
        "pe": 26.0, "pb": 6.5, "ev_ebitda": 18.0,
        "roe": 0.22, "profit_margin": 0.18, "operating_margin": 0.22,
        "debt_to_equity": 12.0,
        "revenue_growth": 0.08, "earnings_growth": 0.10,
        "dividend_yield": 0.020,
    },
    "Banking": {
        # Banks use different metrics (NIM, NPA); PE/PB still useful but D/E
        # is naturally very high. We surface PE/PB/ROE only for banks.
        "pe": 14.0, "pb": 2.2, "ev_ebitda": None,
        "roe": 0.14, "profit_margin": None, "operating_margin": None,
        "debt_to_equity": None,
        "revenue_growth": 0.12, "earnings_growth": 0.15,
        "dividend_yield": 0.012,
    },
    "Financial Services": {
        "pe": 18.0, "pb": 3.5, "ev_ebitda": None,
        "roe": 0.16, "profit_margin": 0.20, "operating_margin": 0.30,
        "debt_to_equity": None,
        "revenue_growth": 0.15, "earnings_growth": 0.18,
        "dividend_yield": 0.010,
    },
    "FMCG": {
        "pe": 52.0, "pb": 11.0, "ev_ebitda": 35.0,
        "roe": 0.45, "profit_margin": 0.16, "operating_margin": 0.22,
        "debt_to_equity": 25.0,
        "revenue_growth": 0.09, "earnings_growth": 0.10,
        "dividend_yield": 0.018,
    },
    "Consumer": {
        "pe": 45.0, "pb": 8.0, "ev_ebitda": 28.0,
        "roe": 0.28, "profit_margin": 0.12, "operating_margin": 0.18,
        "debt_to_equity": 35.0,
        "revenue_growth": 0.10, "earnings_growth": 0.12,
        "dividend_yield": 0.015,
    },
    "Energy": {
        "pe": 12.0, "pb": 1.5, "ev_ebitda": 7.5,
        "roe": 0.12, "profit_margin": 0.08, "operating_margin": 0.12,
        "debt_to_equity": 80.0,
        "revenue_growth": 0.05, "earnings_growth": 0.06,
        "dividend_yield": 0.030,
    },
    "Pharma": {
        "pe": 28.0, "pb": 4.0, "ev_ebitda": 18.0,
        "roe": 0.16, "profit_margin": 0.14, "operating_margin": 0.20,
        "debt_to_equity": 28.0,
        "revenue_growth": 0.10, "earnings_growth": 0.12,
        "dividend_yield": 0.012,
    },
    "Healthcare": {
        "pe": 32.0, "pb": 4.5, "ev_ebitda": 20.0,
        "roe": 0.15, "profit_margin": 0.12, "operating_margin": 0.18,
        "debt_to_equity": 30.0,
        "revenue_growth": 0.12, "earnings_growth": 0.14,
        "dividend_yield": 0.008,
    },
    "Auto": {
        "pe": 22.0, "pb": 3.5, "ev_ebitda": 12.0,
        "roe": 0.14, "profit_margin": 0.07, "operating_margin": 0.10,
        "debt_to_equity": 60.0,
        "revenue_growth": 0.08, "earnings_growth": 0.12,
        "dividend_yield": 0.014,
    },
    "Metal": {
        "pe": 14.0, "pb": 2.0, "ev_ebitda": 8.0,
        "roe": 0.13, "profit_margin": 0.08, "operating_margin": 0.14,
        "debt_to_equity": 70.0,
        "revenue_growth": 0.06, "earnings_growth": 0.08,
        "dividend_yield": 0.022,
    },
    "Telecom": {
        "pe": 30.0, "pb": 4.0, "ev_ebitda": 9.0,
        "roe": 0.10, "profit_margin": 0.05, "operating_margin": 0.20,
        "debt_to_equity": 220.0,
        "revenue_growth": 0.10, "earnings_growth": 0.18,
        "dividend_yield": 0.005,
    },
    "Infrastructure": {
        "pe": 22.0, "pb": 3.0, "ev_ebitda": 14.0,
        "roe": 0.13, "profit_margin": 0.07, "operating_margin": 0.10,
        "debt_to_equity": 110.0,
        "revenue_growth": 0.10, "earnings_growth": 0.14,
        "dividend_yield": 0.012,
    },
    "Realty": {
        "pe": 28.0, "pb": 3.5, "ev_ebitda": 18.0,
        "roe": 0.10, "profit_margin": 0.10, "operating_margin": 0.18,
        "debt_to_equity": 90.0,
        "revenue_growth": 0.15, "earnings_growth": 0.20,
        "dividend_yield": 0.005,
    },
    "Cement": {
        "pe": 24.0, "pb": 3.5, "ev_ebitda": 14.0,
        "roe": 0.12, "profit_margin": 0.10, "operating_margin": 0.18,
        "debt_to_equity": 35.0,
        "revenue_growth": 0.07, "earnings_growth": 0.10,
        "dividend_yield": 0.018,
    },
    "Chemicals": {
        "pe": 28.0, "pb": 4.0, "ev_ebitda": 16.0,
        "roe": 0.15, "profit_margin": 0.10, "operating_margin": 0.16,
        "debt_to_equity": 40.0,
        "revenue_growth": 0.09, "earnings_growth": 0.12,
        "dividend_yield": 0.010,
    },
    "Power": {
        "pe": 16.0, "pb": 2.0, "ev_ebitda": 10.0,
        "roe": 0.11, "profit_margin": 0.10, "operating_margin": 0.20,
        "debt_to_equity": 130.0,
        "revenue_growth": 0.06, "earnings_growth": 0.08,
        "dividend_yield": 0.025,
    },
    "Media": {
        "pe": 22.0, "pb": 3.0, "ev_ebitda": 12.0,
        "roe": 0.10, "profit_margin": 0.08, "operating_margin": 0.14,
        "debt_to_equity": 30.0,
        "revenue_growth": 0.08, "earnings_growth": 0.10,
        "dividend_yield": 0.010,
    },
}

# Aliases — yfinance / NSE sector strings vary; map to canonical buckets above.
SECTOR_ALIASES: dict[str, str] = {
    "Technology": "IT",
    "Information Technology": "IT",
    "Communication Services": "Telecom",
    "Consumer Cyclical": "Consumer",
    "Consumer Defensive": "FMCG",
    "Financial": "Banking",
    "Financials": "Banking",
    "Basic Materials": "Metal",
    "Materials": "Metal",
    "Industrials": "Infrastructure",
    "Real Estate": "Realty",
    "Utilities": "Power",
}


def get_sector_medians(sector: Optional[str]) -> Optional[dict[str, Optional[float]]]:
    """Return curated median fundamentals for a sector, or None if unknown."""
    if not sector:
        return None
    canonical = SECTOR_ALIASES.get(sector, sector)
    return SECTOR_MEDIANS.get(canonical)

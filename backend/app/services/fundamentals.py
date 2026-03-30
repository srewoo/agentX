from __future__ import annotations
"""
Fundamental analysis service using yfinance (free).
Extracts valuation, growth, profitability, financial health, dividend,
and ownership metrics, then computes a 0-10 health score.
"""
import asyncio
import logging
from typing import Any

import yfinance as yf

from app.utils import safe_float

logger = logging.getLogger(__name__)


def _resolve_yf_symbol(symbol: str) -> str:
    """Add .NS suffix for NSE stocks (matching data_fetcher convention)."""
    if symbol.startswith("^") or symbol.endswith(".NS") or symbol.endswith(".BO") or "=" in symbol:
        return symbol
    return f"{symbol}.NS"


def _extract_fundamentals(info: dict[str, Any]) -> dict[str, Any]:
    """Extract and structure fundamental data from a yfinance info dict."""

    # Valuation
    pe = safe_float(info.get("trailingPE"))
    forward_pe = safe_float(info.get("forwardPE"))
    pb = safe_float(info.get("priceToBook"))
    ps = safe_float(info.get("priceToSalesTrailing12Months"))
    ev_ebitda = safe_float(info.get("enterpriseToEbitda"))

    # Growth
    revenue_growth = safe_float(info.get("revenueGrowth"))
    earnings_growth = safe_float(info.get("earningsGrowth"))
    quarterly_earnings_growth = safe_float(info.get("earningsQuarterlyGrowth"))

    # Profitability
    roe = safe_float(info.get("returnOnEquity"))
    roa = safe_float(info.get("returnOnAssets"))
    profit_margin = safe_float(info.get("profitMargins"))
    operating_margin = safe_float(info.get("operatingMargins"))
    gross_margin = safe_float(info.get("grossMargins"))

    # Financial health
    debt_to_equity = safe_float(info.get("debtToEquity"))
    current_ratio = safe_float(info.get("currentRatio"))
    quick_ratio = safe_float(info.get("quickRatio"))
    total_debt = safe_float(info.get("totalDebt"))
    total_cash = safe_float(info.get("totalCash"))

    # Dividends
    dividend_yield = safe_float(info.get("dividendYield"))
    dividend_rate = safe_float(info.get("dividendRate"))
    payout_ratio = safe_float(info.get("payoutRatio"))
    five_yr_avg_div_yield = safe_float(info.get("fiveYearAvgDividendYield"))

    # Ownership
    insider_pct = safe_float(info.get("heldPercentInsiders"))
    institutional_pct = safe_float(info.get("heldPercentInstitutions"))

    # Earnings
    revenue_per_share = safe_float(info.get("revenuePerShare"))
    trailing_eps = safe_float(info.get("trailingEps"))
    forward_eps = safe_float(info.get("forwardEps"))

    return {
        "valuation": {
            "pe": pe,
            "forward_pe": forward_pe,
            "pb": pb,
            "ps": ps,
            "ev_ebitda": ev_ebitda,
        },
        "growth": {
            "revenue_growth": revenue_growth,
            "earnings_growth": earnings_growth,
            "quarterly_earnings_growth": quarterly_earnings_growth,
        },
        "profitability": {
            "roe": roe,
            "roa": roa,
            "profit_margin": profit_margin,
            "operating_margin": operating_margin,
            "gross_margin": gross_margin,
        },
        "financial_health": {
            "debt_to_equity": debt_to_equity,
            "current_ratio": current_ratio,
            "quick_ratio": quick_ratio,
            "total_debt": total_debt,
            "total_cash": total_cash,
        },
        "dividends": {
            "yield": dividend_yield,
            "rate": dividend_rate,
            "payout_ratio": payout_ratio,
            "five_yr_avg_yield": five_yr_avg_div_yield,
        },
        "ownership": {
            "insider_pct": insider_pct,
            "institutional_pct": institutional_pct,
        },
        "earnings": {
            "revenue_per_share": revenue_per_share,
            "trailing_eps": trailing_eps,
            "forward_eps": forward_eps,
        },
    }


def _compute_health_score(data: dict[str, Any]) -> int:
    """
    Compute a fundamental health score from 0 to 10.

    Scoring:
    - PE ratio reasonable (10-25 for India) = +2, edge ranges = +1
    - Positive earnings growth = +1, >10% = +2
    - ROE > 15% = +2, > 10% = +1
    - Debt/Equity < 0.5 = +2, < 1.0 = +1
    - Positive revenue growth = +1
    - Dividend yield > 0 = +1
    """
    score = 0

    # PE scoring
    pe = data["valuation"]["pe"]
    if pe is not None:
        if 10 <= pe <= 25:
            score += 2
        elif (5 <= pe < 10) or (25 < pe <= 40):
            score += 1
        # pe < 5 or pe > 40 => 0

    # Earnings growth scoring
    eg = data["growth"]["earnings_growth"]
    if eg is not None:
        if eg > 0.10:
            score += 2
        elif eg > 0:
            score += 1

    # ROE scoring (yfinance returns as decimal, e.g. 0.15 = 15%)
    roe = data["profitability"]["roe"]
    if roe is not None:
        if roe > 0.15:
            score += 2
        elif roe > 0.10:
            score += 1

    # Debt/Equity scoring (yfinance returns as percentage, e.g. 50.0 = 0.5 ratio)
    # Some yfinance versions return it as a ratio, others as percentage.
    # We normalise: if > 10 assume percentage, divide by 100.
    de = data["financial_health"]["debt_to_equity"]
    if de is not None:
        de_ratio = de / 100.0 if de > 10 else de
        if de_ratio < 0.5:
            score += 2
        elif de_ratio < 1.0:
            score += 1

    # Revenue growth scoring
    rg = data["growth"]["revenue_growth"]
    if rg is not None and rg > 0:
        score += 1

    # Dividend yield scoring
    dy = data["dividends"]["yield"]
    if dy is not None and dy > 0:
        score += 1

    return min(score, 10)


def _score_to_signal(score: int) -> str:
    """Map a 0-10 score to a human-readable signal."""
    if score >= 8:
        return "Strong"
    if score >= 6:
        return "Good"
    if score >= 4:
        return "Fair"
    if score >= 2:
        return "Weak"
    return "Poor"


def _fetch_info_sync(yf_symbol: str) -> dict[str, Any]:
    """Synchronous yfinance info fetch (runs in executor)."""
    try:
        ticker = yf.Ticker(yf_symbol)
        info = ticker.info
        if not info or not isinstance(info, dict):
            return {}
        return info
    except Exception as e:
        logger.warning("yfinance info fetch failed for %s: %s", yf_symbol, e)
        return {}


async def get_fundamentals(symbol: str) -> dict[str, Any]:
    """
    Extract fundamental data from yfinance for a stock.
    Returns a dict with fundamental metrics and a health score (0-10).
    """
    yf_symbol = _resolve_yf_symbol(symbol)

    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, _fetch_info_sync, yf_symbol)

    if not info:
        logger.warning("Empty info dict for %s, returning defaults", symbol)
        return _empty_result(symbol)

    data = _extract_fundamentals(info)
    score = _compute_health_score(data)
    signal = _score_to_signal(score)

    return {
        "symbol": symbol,
        **data,
        "fundamental_score": score,
        "fundamental_signal": signal,
    }


def _empty_result(symbol: str) -> dict[str, Any]:
    """Return a zeroed-out fundamentals dict when data is unavailable."""
    return {
        "symbol": symbol,
        "valuation": {"pe": None, "forward_pe": None, "pb": None, "ps": None, "ev_ebitda": None},
        "growth": {"revenue_growth": None, "earnings_growth": None, "quarterly_earnings_growth": None},
        "profitability": {"roe": None, "roa": None, "profit_margin": None, "operating_margin": None, "gross_margin": None},
        "financial_health": {"debt_to_equity": None, "current_ratio": None, "quick_ratio": None, "total_debt": None, "total_cash": None},
        "dividends": {"yield": None, "rate": None, "payout_ratio": None, "five_yr_avg_yield": None},
        "ownership": {"insider_pct": None, "institutional_pct": None},
        "earnings": {"revenue_per_share": None, "trailing_eps": None, "forward_eps": None},
        "fundamental_score": 0,
        "fundamental_signal": "Poor",
    }

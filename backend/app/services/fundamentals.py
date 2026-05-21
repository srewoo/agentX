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


def _resolve_yf_symbol(symbol: str, exchange: str = "NSE") -> str:
    """Add the appropriate exchange suffix for yfinance lookup."""
    if symbol.startswith("^") or symbol.endswith(".NS") or symbol.endswith(".BO") or "=" in symbol:
        return symbol
    return f"{symbol}.BO" if exchange.upper() == "BSE" else f"{symbol}.NS"


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
        # Wire-shape compatible with FundamentalsCard / FundamentalsResponse.
        # The card reads `dividend_yield` / `dividend_rate`; older callers
        # still relied on `yield`/`rate`, so we expose both for one release.
        "dividends": {
            "dividend_yield": dividend_yield,
            "dividend_rate": dividend_rate,
            "payout_ratio": payout_ratio,
            "five_yr_avg_yield": five_yr_avg_div_yield,
            "yield": dividend_yield,
            "rate": dividend_rate,
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


_YFINANCE_TIMEOUT = 30  # seconds — prevent yfinance from hanging indefinitely


async def get_fundamentals(symbol: str, exchange: str = "NSE") -> dict[str, Any]:
    """
    Extract fundamental data for a stock with a layered fallback chain:

      1. yfinance (broadest coverage when Yahoo isn't throttling)
      2. NSE quote (sector/symbol PE, industry — bulletproof but limited)
      3. screener.in (full ratio set — primary fallback for ROE / D/E etc.)

    Each later source only fills fields the earlier ones left empty.
    Returns the canonical fundamentals dict + health score.

    ``exchange`` picks the yfinance suffix: NSE → .NS, BSE → .BO. The NSE
    quote fallback is skipped on BSE since the endpoint is NSE-only.
    """
    yf_symbol = _resolve_yf_symbol(symbol, exchange)

    loop = asyncio.get_event_loop()
    info: dict[str, Any] = {}
    # Two attempts with a short backoff. Yahoo 429s are transient; a single
    # retry typically recovers without hammering the upstream further.
    for attempt in (0, 1):
        try:
            info = await asyncio.wait_for(
                loop.run_in_executor(None, _fetch_info_sync, yf_symbol),
                timeout=_YFINANCE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("yfinance info fetch timed out after %ds for %s", _YFINANCE_TIMEOUT, symbol)
            info = {}
        if info:
            break
        if attempt == 0:
            await asyncio.sleep(0.8)

    if info:
        data = _extract_fundamentals(info)
        score = _compute_health_score(data)
        signal = _score_to_signal(score)
        primary: dict[str, Any] = {
            "symbol": symbol,
            **data,
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "health_score": score,
            "signal": signal,
            "fundamental_score": score,
            "fundamental_signal": signal,
        }
    else:
        logger.warning("Empty info dict for %s after retry, falling back", symbol)
        primary = _empty_result(symbol)

    # ── Fallback chain ────────────────────────────────────────────────
    # Each fallback runs in a thread so we don't block the event loop.
    from app.services.fundamentals_fallbacks import (
        fetch_nse_quote,
        fetch_screener_in,
        merge_fundamentals,
    )

    nse_partial = None
    screener_partial = None
    # NSE quote fallback only applies to NSE-listed symbols.
    if exchange.upper() != "BSE":
        try:
            nse_partial = await asyncio.wait_for(
                loop.run_in_executor(None, fetch_nse_quote, symbol),
                timeout=10,
            )
        except Exception as e:
            logger.debug("nse_quote fallback skipped for %s: %s", symbol, e)

    try:
        screener_partial = await asyncio.wait_for(
            loop.run_in_executor(None, fetch_screener_in, symbol),
            timeout=15,
        )
    except Exception as e:
        logger.debug("screener.in fallback skipped for %s: %s", symbol, e)

    merged = merge_fundamentals(primary, nse_partial, screener_partial)

    # Re-score against merged data — fallbacks may have populated PE / ROE /
    # growth fields that the original score didn't see.
    if not info:
        try:
            new_score = _compute_health_score(merged)
            merged["health_score"] = new_score
            merged["signal"] = _score_to_signal(new_score)
            merged["fundamental_score"] = new_score
            merged["fundamental_signal"] = _score_to_signal(new_score)
        except Exception:
            pass
    if any((merged.get(n) or {}) for n in ("valuation", "profitability", "growth")):
        merged.pop("error", None)

    return merged


def _empty_result(symbol: str) -> dict[str, Any]:
    """Return a zeroed-out fundamentals dict when data is unavailable.

    `error` is populated so the UI can distinguish "Yahoo upstream throttled
    us" from "stock genuinely has no fundamentals" instead of just rendering
    a wall of em-dashes.
    """
    return {
        "symbol": symbol,
        "valuation": {"pe": None, "forward_pe": None, "pb": None, "ps": None, "ev_ebitda": None},
        "growth": {"revenue_growth": None, "earnings_growth": None, "quarterly_earnings_growth": None},
        "profitability": {"roe": None, "roa": None, "profit_margin": None, "operating_margin": None, "gross_margin": None},
        "financial_health": {"debt_to_equity": None, "current_ratio": None, "quick_ratio": None, "total_debt": None, "total_cash": None},
        "dividends": {"dividend_yield": None, "dividend_rate": None, "payout_ratio": None, "five_yr_avg_yield": None, "yield": None, "rate": None},
        "ownership": {"insider_pct": None, "institutional_pct": None},
        "earnings": {"revenue_per_share": None, "trailing_eps": None, "forward_eps": None},
        "sector": None,
        "industry": None,
        "health_score": 0,
        "signal": "Unavailable",
        "fundamental_score": 0,
        "fundamental_signal": "Poor",
        "error": "Fundamentals upstream (Yahoo) is currently rate-limiting requests. Try again in a minute.",
    }

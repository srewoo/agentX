from __future__ import annotations
"""Deep fundamental analysis — cash flow, balance sheet, earnings quality, moat.

Adds substance to the existing binary PE/ROE rubric in `fundamentals.py`:

  • cash_flow:        FCF/share, FCF yield, FCF growth, OCF/NI ratio
  • balance_sheet:    interest coverage, net-debt/EBITDA, current ratio trend
  • earnings_quality: Sloan accruals proxy, OCF-NI gap, EPS volatility,
                     gross-margin stability
  • moat_proxies:     gross-margin level, ROIC, FCF-margin persistence
  • composite_score:  0..100 — used as a quality multiplier on conviction

Reads from yfinance financials/cashflow/balance_sheet (already cached by
yfinance internally). Falls back gracefully if any frame is missing —
unlike the existing rubric, every sub-score returns `None` instead of
silently treating missing data as zero, so the consumer can see *why*
the composite is low.
"""
import asyncio
import logging
from statistics import mean, pstdev
from typing import Any, Optional

import yfinance as yf

logger = logging.getLogger(__name__)

_YF_TIMEOUT = 30


def _safe(v: Any) -> Optional[float]:
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except Exception:
        return None


def _first_row(df, candidates: list[str]) -> Optional[list[float]]:
    """Return the first matching row as a list of period values (latest first)."""
    if df is None or df.empty:
        return None
    idx_norm = {str(i).strip().lower(): i for i in df.index}
    for c in candidates:
        k = c.strip().lower()
        if k in idx_norm:
            vals = df.loc[idx_norm[k]].tolist()
            return [_safe(v) for v in vals]
    return None


def _yoy_growth(series: list[Optional[float]]) -> Optional[float]:
    """latest / next-latest − 1, with sign handling for negative bases."""
    if not series or len(series) < 2:
        return None
    a, b = series[0], series[1]
    if a is None or b is None or b == 0:
        return None
    return (a - b) / abs(b)


def _stdev_pct(series: list[Optional[float]]) -> Optional[float]:
    vals = [v for v in series if v is not None]
    if len(vals) < 3:
        return None
    m = mean(vals)
    if m == 0:
        return None
    return pstdev(vals) / abs(m)


def _cash_flow_metrics(cf, bs, info: dict[str, Any]) -> dict[str, Optional[float]]:
    ocf = _first_row(cf, ["Operating Cash Flow", "Total Cash From Operating Activities", "Cash Flow From Operations"])
    capex = _first_row(cf, ["Capital Expenditure", "Capital Expenditures"])
    fcf = None
    if ocf and capex and ocf[0] is not None and capex[0] is not None:
        # yfinance stores capex as negative.
        fcf_series = [
            (o + c) if (o is not None and c is not None) else None
            for o, c in zip(ocf, capex)
        ]
        fcf = fcf_series[0]
        fcf_growth = _yoy_growth(fcf_series)
    else:
        fcf_growth = None

    shares = _safe(info.get("sharesOutstanding"))
    mcap = _safe(info.get("marketCap"))
    ni = _first_row(cf, ["Net Income"]) or [None]

    return {
        "fcf": fcf,
        "fcf_per_share": (fcf / shares) if (fcf is not None and shares) else None,
        "fcf_yield": (fcf / mcap) if (fcf is not None and mcap) else None,
        "fcf_growth_yoy": fcf_growth,
        # OCF/NI > 1 → earnings are backed by real cash; < 0.7 is a red flag.
        "ocf_to_ni": (ocf[0] / ni[0]) if (ocf and ocf[0] is not None and ni[0] not in (None, 0)) else None,
    }


def _balance_sheet_metrics(bs, fin, info: dict[str, Any]) -> dict[str, Optional[float]]:
    total_debt = _safe(info.get("totalDebt"))
    cash = _safe(info.get("totalCash"))
    net_debt = (total_debt - cash) if (total_debt is not None and cash is not None) else None
    ebitda = _safe(info.get("ebitda"))
    ebit = _first_row(fin, ["EBIT", "Operating Income"]) or [None]
    interest = _first_row(fin, ["Interest Expense"]) or [None]

    interest_coverage = None
    if ebit[0] is not None and interest[0] not in (None, 0):
        # Interest is reported negative; flip sign.
        interest_coverage = ebit[0] / abs(interest[0])

    current_ratio_trend = None
    ca = _first_row(bs, ["Current Assets", "Total Current Assets"])
    cl = _first_row(bs, ["Current Liabilities", "Total Current Liabilities"])
    if ca and cl and len(ca) >= 2 and ca[0] is not None and ca[1] is not None and cl[0] and cl[1]:
        latest = ca[0] / cl[0]
        prev = ca[1] / cl[1]
        current_ratio_trend = latest - prev  # > 0 means liquidity improving

    return {
        "net_debt": net_debt,
        "net_debt_to_ebitda": (net_debt / ebitda) if (net_debt is not None and ebitda not in (None, 0)) else None,
        "interest_coverage": interest_coverage,
        "current_ratio_trend": current_ratio_trend,
        "debt_to_assets": _safe(info.get("totalDebt", 0) / info["totalAssets"]) if info.get("totalAssets") else None,
    }


def _earnings_quality(cf, fin, bs) -> dict[str, Optional[float]]:
    """Sloan-accruals-style proxy. High accruals → low-quality earnings."""
    ni_row = _first_row(fin, ["Net Income"]) or _first_row(cf, ["Net Income"])
    ocf_row = _first_row(cf, ["Operating Cash Flow", "Total Cash From Operating Activities"])
    # Accruals = (NI - OCF) / avg total assets. yfinance gives total assets in bs.
    ta_row = _first_row(bs, ["Total Assets"])
    sloan = None
    if ni_row and ocf_row and ta_row and ni_row[0] is not None and ocf_row[0] is not None and ta_row[0]:
        avg_ta = ta_row[0]
        if len(ta_row) >= 2 and ta_row[1]:
            avg_ta = (ta_row[0] + ta_row[1]) / 2
        sloan = (ni_row[0] - ocf_row[0]) / avg_ta

    # EPS volatility — high std/mean over 4 years means unpredictable earnings.
    eps_row = _first_row(fin, ["Diluted EPS", "Basic EPS"])
    eps_vol = _stdev_pct(eps_row) if eps_row else None

    # Gross-margin stability — moat proxy.
    rev = _first_row(fin, ["Total Revenue", "Revenue"])
    gp = _first_row(fin, ["Gross Profit"])
    margins = []
    if rev and gp:
        for r, g in zip(rev, gp):
            if r and g is not None and r != 0:
                margins.append(g / r)
    margin_vol = _stdev_pct(margins) if margins else None
    avg_margin = mean(margins) if margins else None

    return {
        "sloan_accruals": sloan,                  # > 0.10 = aggressive accruals
        "ocf_ni_gap_severe": (sloan is not None and abs(sloan) > 0.10) or None,
        "eps_volatility": eps_vol,                 # > 0.5 = unpredictable
        "gross_margin_avg": avg_margin,
        "gross_margin_volatility": margin_vol,     # < 0.10 = stable moat
    }


def _moat_score(info: dict[str, Any], earn_q: dict[str, Any]) -> dict[str, Optional[float]]:
    """Composite proxies: durable competitive advantage signals."""
    roic = _safe(info.get("returnOnEquity"))  # yfinance lacks true ROIC; ROE is the proxy
    gm_avg = earn_q.get("gross_margin_avg")
    gm_vol = earn_q.get("gross_margin_volatility")
    # Wide-moat businesses typically run gross margin > 35% with low volatility.
    moat_score = 0.0
    parts = 0
    if gm_avg is not None:
        moat_score += 1.0 if gm_avg > 0.40 else (0.5 if gm_avg > 0.25 else 0.0)
        parts += 1
    if gm_vol is not None:
        moat_score += 1.0 if gm_vol < 0.10 else (0.5 if gm_vol < 0.20 else 0.0)
        parts += 1
    if roic is not None:
        moat_score += 1.0 if roic > 0.20 else (0.5 if roic > 0.12 else 0.0)
        parts += 1
    return {
        "moat_score": round(moat_score / parts, 3) if parts else None,
        "wide_moat": bool(parts and moat_score / parts >= 0.75),
    }


def _composite_quality(
    cash: dict[str, Any], bal: dict[str, Any], earn_q: dict[str, Any], moat: dict[str, Any],
) -> int:
    """0..100 quality score combining cash flow, balance sheet, earnings, moat.

    Each dimension contributes ≤25 and only when its underlying data exists.
    If everything is None we return 0 — caller treats as "unrated".
    """
    score = 0.0
    available = 0

    # Cash flow (25)
    cf_pts = 0.0; cf_max = 0
    if cash.get("ocf_to_ni") is not None:
        cf_pts += 10 if cash["ocf_to_ni"] > 1.0 else (5 if cash["ocf_to_ni"] > 0.7 else 0)
        cf_max += 10
    if cash.get("fcf_yield") is not None:
        cf_pts += 10 if cash["fcf_yield"] > 0.05 else (5 if cash["fcf_yield"] > 0.02 else 0)
        cf_max += 10
    if cash.get("fcf_growth_yoy") is not None:
        cf_pts += 5 if cash["fcf_growth_yoy"] > 0.10 else (2.5 if cash["fcf_growth_yoy"] > 0 else 0)
        cf_max += 5
    if cf_max:
        score += cf_pts / cf_max * 25; available += 25

    # Balance sheet (25)
    bs_pts = 0.0; bs_max = 0
    if bal.get("interest_coverage") is not None:
        bs_pts += 10 if bal["interest_coverage"] > 5 else (5 if bal["interest_coverage"] > 2 else 0)
        bs_max += 10
    if bal.get("net_debt_to_ebitda") is not None:
        nd = bal["net_debt_to_ebitda"]
        bs_pts += 10 if nd < 1 else (5 if nd < 3 else 0)
        bs_max += 10
    if bal.get("current_ratio_trend") is not None:
        bs_pts += 5 if bal["current_ratio_trend"] >= 0 else 0
        bs_max += 5
    if bs_max:
        score += bs_pts / bs_max * 25; available += 25

    # Earnings quality (25)
    eq_pts = 0.0; eq_max = 0
    if earn_q.get("sloan_accruals") is not None:
        eq_pts += 10 if abs(earn_q["sloan_accruals"]) < 0.05 else (5 if abs(earn_q["sloan_accruals"]) < 0.10 else 0)
        eq_max += 10
    if earn_q.get("eps_volatility") is not None:
        eq_pts += 10 if earn_q["eps_volatility"] < 0.3 else (5 if earn_q["eps_volatility"] < 0.6 else 0)
        eq_max += 10
    if earn_q.get("gross_margin_volatility") is not None:
        eq_pts += 5 if earn_q["gross_margin_volatility"] < 0.10 else (2.5 if earn_q["gross_margin_volatility"] < 0.20 else 0)
        eq_max += 5
    if eq_max:
        score += eq_pts / eq_max * 25; available += 25

    # Moat (25)
    if moat.get("moat_score") is not None:
        score += moat["moat_score"] * 25; available += 25

    if available == 0:
        return 0
    # Normalise to 0..100 across only the dimensions we had data for.
    return int(round(score / available * 100))


def _fetch_sync(yf_symbol: str) -> dict[str, Any]:
    """Single yfinance call that pulls financials + cashflow + balance_sheet.

    yfinance caches these internally per Ticker, so one Ticker reuses the
    connection across the three frames. Returns dict of raw DataFrames.
    """
    try:
        t = yf.Ticker(yf_symbol)
        return {
            "info": t.info or {},
            "financials": t.financials,         # income statement
            "cashflow": t.cashflow,
            "balance_sheet": t.balance_sheet,
        }
    except Exception as e:
        logger.warning("yfinance deep-fundamentals fetch failed for %s: %s", yf_symbol, e)
        return {"info": {}, "financials": None, "cashflow": None, "balance_sheet": None}


async def get_deep_fundamentals(symbol: str, exchange: str = "NSE") -> dict[str, Any]:
    """Async wrapper. Returns the four sub-dicts + composite score.

    Safe to call alongside the existing `get_fundamentals` — they hit
    different yfinance endpoints and merge cleanly.
    """
    yf_sym = symbol if any(symbol.endswith(s) for s in (".NS", ".BO")) else (
        f"{symbol}.BO" if exchange.upper() == "BSE" else f"{symbol}.NS"
    )
    loop = asyncio.get_event_loop()
    try:
        bundle = await asyncio.wait_for(
            loop.run_in_executor(None, _fetch_sync, yf_sym),
            timeout=_YF_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("deep fundamentals timeout for %s", symbol)
        return {"symbol": symbol, "error": "timeout", "composite_score": 0}

    info = bundle.get("info") or {}
    cash = _cash_flow_metrics(bundle["cashflow"], bundle["balance_sheet"], info)
    bal = _balance_sheet_metrics(bundle["balance_sheet"], bundle["financials"], info)
    earn_q = _earnings_quality(bundle["cashflow"], bundle["financials"], bundle["balance_sheet"])
    moat = _moat_score(info, earn_q)
    composite = _composite_quality(cash, bal, earn_q, moat)

    return {
        "symbol": symbol,
        "cash_flow": cash,
        "balance_sheet": bal,
        "earnings_quality": earn_q,
        "moat": moat,
        "composite_score": composite,
        "tier": (
            "elite" if composite >= 80 else
            "high_quality" if composite >= 65 else
            "average" if composite >= 45 else
            "low_quality" if composite >= 25 else
            "unrated" if composite == 0 else "poor"
        ),
    }

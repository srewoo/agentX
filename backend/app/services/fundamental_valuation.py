from __future__ import annotations

"""Fundamental + valuation scoring for recommendation evidence.

This module is deliberately pure: it consumes the existing fundamentals dict
and curated sector medians, then returns a compact evidence object that the
recommendation ensemble and LLM judge can both use.
"""

from typing import Any

from app.services.sector_medians import get_sector_medians


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if out == out else None
    except Exception:
        return None


def _clip(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.1f}%"


def _de_ratio(value: float | None) -> float | None:
    if value is None:
        return None
    return value / 100.0 if value > 10 else value


def _relative_discount(value: float | None, median: float | None) -> float:
    """Positive means cheaper than sector; negative means expensive."""
    if value is None or median is None or value <= 0 or median <= 0:
        return 0.0
    return _clip((median - value) / median)


def analyze_fundamental_valuation(
    fundamentals: dict[str, Any] | None,
    *,
    sector: str | None,
) -> dict[str, Any]:
    if not fundamentals:
        return {
            "available": False,
            "score": 50,
            "normalized_score": 0.0,
            "grade": "UNAVAILABLE",
            "quality_score": 0,
            "valuation_score": 0,
            "growth_score": 0,
            "balance_sheet_score": 0,
            "income_score": 0,
            "reasons": ["Fundamental data unavailable."],
            "red_flags": ["Do not upgrade conviction from fundamentals."],
            "sector_medians": get_sector_medians(sector) or {},
        }

    valuation = fundamentals.get("valuation") or {}
    growth = fundamentals.get("growth") or {}
    profitability = fundamentals.get("profitability") or {}
    health = fundamentals.get("financial_health") or {}
    dividends = fundamentals.get("dividends") or {}
    med = get_sector_medians(sector) or {}

    pe = _num(valuation.get("pe"))
    pb = _num(valuation.get("pb"))
    ev_ebitda = _num(valuation.get("ev_ebitda"))
    roe = _num(profitability.get("roe"))
    margin = _num(profitability.get("profit_margin"))
    op_margin = _num(profitability.get("operating_margin"))
    revenue_growth = _num(growth.get("revenue_growth"))
    earnings_growth = _num(growth.get("earnings_growth"))
    debt_equity = _de_ratio(_num(health.get("debt_to_equity")))
    dividend_yield = _num(dividends.get("dividend_yield") or dividends.get("yield"))

    valuation_score = 0.0
    valuation_score += 0.45 * _relative_discount(pe, _num(med.get("pe")))
    valuation_score += 0.25 * _relative_discount(pb, _num(med.get("pb")))
    valuation_score += 0.20 * _relative_discount(ev_ebitda, _num(med.get("ev_ebitda")))
    if pe is not None:
        if 8 <= pe <= 28:
            valuation_score += 0.20
        elif pe > 70 or pe <= 0:
            valuation_score -= 0.35
    valuation_score = _clip(valuation_score)

    quality_score = 0.0
    med_roe = _num(med.get("roe"))
    if roe is not None:
        quality_score += 0.35 if roe >= 0.18 else (0.15 if roe >= 0.12 else -0.25 if roe < 0 else 0)
        if med_roe is not None:
            quality_score += _clip((roe - med_roe) / max(0.05, abs(med_roe))) * 0.25
    med_margin = _num(med.get("profit_margin"))
    if margin is not None and med_margin is not None:
        quality_score += _clip((margin - med_margin) / max(0.05, abs(med_margin))) * 0.15
    if op_margin is not None and op_margin > 0.12:
        quality_score += 0.10
    quality_score = _clip(quality_score)

    growth_score = 0.0
    if revenue_growth is not None:
        growth_score += 0.25 if revenue_growth > 0.10 else (0.10 if revenue_growth > 0 else -0.15)
    if earnings_growth is not None:
        growth_score += 0.35 if earnings_growth > 0.12 else (0.15 if earnings_growth > 0 else -0.25)
    growth_score = _clip(growth_score)

    balance_sheet_score = 0.0
    if debt_equity is not None:
        if debt_equity < 0.5:
            balance_sheet_score += 0.45
        elif debt_equity < 1.2:
            balance_sheet_score += 0.20
        elif debt_equity > 2.0:
            balance_sheet_score -= 0.35
    balance_sheet_score = _clip(balance_sheet_score)

    income_score = 0.0
    if dividend_yield is not None:
        if 0.005 <= dividend_yield <= 0.05:
            income_score += 0.15
        elif dividend_yield > 0.08:
            income_score -= 0.10
    income_score = _clip(income_score)

    normalized = _clip(
        0.30 * quality_score
        + 0.25 * valuation_score
        + 0.20 * growth_score
        + 0.20 * balance_sheet_score
        + 0.05 * income_score
    )
    score = int(round(50 + normalized * 50))
    grade = "A" if score >= 75 else "B" if score >= 60 else "C" if score >= 45 else "D"

    reasons: list[str] = []
    red_flags: list[str] = []
    if pe is not None:
        median_pe = _num(med.get("pe"))
        if median_pe and pe < median_pe:
            reasons.append(f"P/E {pe:.1f} is below sector median {median_pe:.1f}.")
        elif median_pe and pe > median_pe * 1.5:
            red_flags.append(f"P/E {pe:.1f} is expensive versus sector median {median_pe:.1f}.")
    if roe is not None:
        if roe >= 0.18:
            reasons.append(f"ROE is strong at {_pct(roe)}.")
        elif roe < 0:
            red_flags.append(f"ROE is negative at {_pct(roe)}.")
    if debt_equity is not None and debt_equity > 2.0:
        red_flags.append(f"Debt/equity is high at {debt_equity:.2f}x.")
    if earnings_growth is not None and earnings_growth < 0:
        red_flags.append(f"Earnings growth is negative at {_pct(earnings_growth)}.")
    if not reasons:
        reasons.append("Fundamentals are neutral versus available sector context.")

    return {
        "available": True,
        "score": score,
        "normalized_score": round(normalized, 4),
        "grade": grade,
        "quality_score": round(quality_score, 4),
        "valuation_score": round(valuation_score, 4),
        "growth_score": round(growth_score, 4),
        "balance_sheet_score": round(balance_sheet_score, 4),
        "income_score": round(income_score, 4),
        "reasons": reasons[:5],
        "red_flags": red_flags[:5],
        "sector_medians": med,
        "snapshot": {
            "pe": pe,
            "pb": pb,
            "ev_ebitda": ev_ebitda,
            "roe": roe,
            "profit_margin": margin,
            "revenue_growth": revenue_growth,
            "earnings_growth": earnings_growth,
            "debt_to_equity": debt_equity,
            "dividend_yield": dividend_yield,
        },
    }

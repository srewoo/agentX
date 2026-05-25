from __future__ import annotations
"""Portfolio-level risk: correlation, VaR, exposure budget.

`risk_manager.py` already covers per-trade ATR sizing and portfolio
*heat* (sum of stop-loss distances). What it lacked:

  • Cross-asset CORRELATION — five "independent" BUYs on bank stocks
    are not five bets, they're one bet five times over.
  • PORTFOLIO VaR — parametric (variance-covariance) and historical
    (empirical) 1-day 95% VaR over the candidate basket.
  • EXPOSURE BUDGET — per-sector, per-direction caps and a max-correlated
    cluster cap; recommendations are dropped/demoted when adding them
    would violate the budget.

All pure functions. Input is the list of `Recommendation` dicts and a
pre-fetched daily-returns matrix (cheap — we already fetched history
for technicals). Output is the same list with `portfolio_context`
populated and recommendations beyond the budget demoted to HOLD with a
reason.
"""
import logging
import math
from collections import defaultdict
from statistics import mean, pstdev
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Caps ────────────────────────────────────────────────────────────────
MAX_DIRECTIONAL_POSITIONS = 8         # top-N BUY+SELL the basket can hold
MAX_SAME_SECTOR = 2                    # mirrors recommendation.MAX_PER_SECTOR
MAX_HIGH_CORRELATION_CLUSTER = 2       # at most 2 picks in a corr-cluster (ρ > 0.7)
MAX_DIRECTIONAL_GROSS_PCT = 60.0       # capital % allocatable to all directional picks
PORTFOLIO_VAR_BUDGET_PCT = 4.0         # 1-day 95% VaR must stay below this

_HIGH_CORR_THRESHOLD = 0.70


# ── Correlation ─────────────────────────────────────────────────────────

def _pct_returns(prices: list[float]) -> list[float]:
    out = []
    for i in range(1, len(prices)):
        prev = prices[i - 1]
        if prev:
            out.append((prices[i] - prev) / prev)
    return out


def _pearson(a: list[float], b: list[float]) -> Optional[float]:
    n = min(len(a), len(b))
    if n < 10:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = mean(a), mean(b)
    sa, sb = pstdev(a), pstdev(b)
    if sa < 1e-9 or sb < 1e-9:
        return None
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / n
    return max(-1.0, min(1.0, cov / (sa * sb)))


def correlation_matrix(returns_by_symbol: dict[str, list[float]]) -> dict[str, dict[str, float]]:
    """Pairwise Pearson correlation of daily returns. Symmetric, ρ_ii=1."""
    syms = list(returns_by_symbol.keys())
    M: dict[str, dict[str, float]] = {s: {s: 1.0} for s in syms}
    for i, a in enumerate(syms):
        for b in syms[i + 1:]:
            p = _pearson(returns_by_symbol[a], returns_by_symbol[b])
            if p is None:
                continue
            M[a][b] = round(p, 3)
            M[b][a] = round(p, 3)
    return M


def correlation_clusters(corr: dict[str, dict[str, float]], threshold: float = _HIGH_CORR_THRESHOLD) -> list[set[str]]:
    """Union-find on |ρ| ≥ threshold edges. Returns list of clusters."""
    parent: dict[str, str] = {s: s for s in corr}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, row in corr.items():
        for b, p in row.items():
            if a != b and abs(p) >= threshold:
                union(a, b)

    groups: dict[str, set[str]] = defaultdict(set)
    for s in corr:
        groups[find(s)].add(s)
    return [g for g in groups.values() if len(g) > 1]


# ── VaR ─────────────────────────────────────────────────────────────────

def parametric_var(
    weights: dict[str, float],
    returns_by_symbol: dict[str, list[float]],
    *,
    confidence: float = 0.95,
) -> Optional[float]:
    """Variance-covariance 1-day VaR at the given confidence.

    `weights` are capital fractions per symbol (negative for SELL).
    Returns VaR as a positive percentage of capital. None when no
    overlapping return history exists.
    """
    syms = [s for s in weights if s in returns_by_symbol and len(returns_by_symbol[s]) >= 10]
    if not syms:
        return None
    n = min(len(returns_by_symbol[s]) for s in syms)
    if n < 30:
        return None
    rets = {s: returns_by_symbol[s][-n:] for s in syms}
    means = {s: mean(rets[s]) for s in syms}

    # Portfolio mean & variance.
    pm = sum(weights[s] * means[s] for s in syms)
    pv = 0.0
    for i, a in enumerate(syms):
        for b in syms:
            cov = sum((rets[a][k] - means[a]) * (rets[b][k] - means[b]) for k in range(n)) / n
            pv += weights[a] * weights[b] * cov
    if pv < 0:
        pv = 0.0
    sigma = math.sqrt(pv)

    # Normal-distribution quantile (z-score) for the confidence level.
    # Standard inverse-CDF approximation (Beasley–Springer / Moro).
    z = _inv_normal_cdf(confidence)
    # VaR is the *loss*, expressed as positive percent of capital.
    var_pct = (z * sigma - pm) * 100.0
    return round(max(0.0, var_pct), 4)


def historical_var(
    weights: dict[str, float],
    returns_by_symbol: dict[str, list[float]],
    *,
    confidence: float = 0.95,
) -> Optional[float]:
    """Empirical VaR — percentile of portfolio's daily return distribution."""
    syms = [s for s in weights if s in returns_by_symbol and len(returns_by_symbol[s]) >= 30]
    if not syms:
        return None
    n = min(len(returns_by_symbol[s]) for s in syms)
    daily = []
    for k in range(n):
        r = sum(weights[s] * returns_by_symbol[s][-n + k] for s in syms)
        daily.append(r)
    daily.sort()
    cut = int((1 - confidence) * len(daily))
    # 95% VaR = 5th percentile loss.
    return round(max(0.0, -daily[cut] * 100.0), 4)


def _inv_normal_cdf(p: float) -> float:
    """Acklam's approximation. Accurate to ~1e-9 in [0.001, 0.999]."""
    # Coefficients
    a = [-3.969683028665376e1, 2.209460984245205e2, -2.759285104469687e2,
         1.383577518672690e2, -3.066479806614716e1, 2.506628277459239]
    b = [-5.447609879822406e1, 1.615858368580409e2, -1.556989798598866e2,
         6.680131188771972e1, -1.328068155288572e1]
    c = [-7.784894002430293e-3, -3.223964580411365e-1, -2.400758277161838,
         -2.549732539343734, 4.374664141464968, 2.938163982698783]
    d = [7.784695709041462e-3, 3.224671290700398e-1, 2.445134137142996,
         3.754408661907416]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
               ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5]) * q / \
               (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
            ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)


# ── Exposure budget ─────────────────────────────────────────────────────

def enforce_exposure_budget(
    recommendations: list[dict[str, Any]],
    *,
    returns_by_symbol: Optional[dict[str, list[float]]] = None,
    capital: float = 100.0,
    risk_per_trade_pct: float = 1.0,
) -> dict[str, Any]:
    """Drop / demote recommendations until all caps are satisfied.

    Caps enforced (in order):
      1. Per-sector position count (`MAX_SAME_SECTOR`)
      2. Per-correlation-cluster count (`MAX_HIGH_CORRELATION_CLUSTER`)
      3. Top-N directional bets (`MAX_DIRECTIONAL_POSITIONS`)
      4. Gross directional capital allocation (`MAX_DIRECTIONAL_GROSS_PCT`)
      5. Portfolio VaR budget (`PORTFOLIO_VAR_BUDGET_PCT`)

    Recommendations that violate a cap are demoted to HOLD with the
    reason appended. Input `recommendations` should be plain dicts (call
    `.model_dump()` if you have Pydantic models). Returns a dict with
    `kept`, `demoted`, and `portfolio_context` for inspection.
    """
    if not recommendations:
        return {"kept": [], "demoted": [], "portfolio_context": {}}

    directional = sorted(
        [r for r in recommendations if r.get("action") in ("BUY", "SELL")],
        key=lambda r: (r.get("conviction", 0), r.get("risk_reward", 0)),
        reverse=True,
    )
    sector_count: dict[str, int] = defaultdict(int)
    demoted: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []

    # 1 & 3 — sector + top-N.
    for r in directional:
        sec = (r.get("sector") or "N/A").lower()
        if sector_count[sec] >= MAX_SAME_SECTOR:
            demoted.append({**r, "demotion_reason": "sector_cap"})
            continue
        if len(kept) >= MAX_DIRECTIONAL_POSITIONS:
            demoted.append({**r, "demotion_reason": "topN_cap"})
            continue
        sector_count[sec] += 1
        kept.append(r)

    # 2 — correlation clusters. Only meaningful when we have returns data.
    clusters: list[set[str]] = []
    if returns_by_symbol:
        kept_syms = [r["symbol"] for r in kept]
        rets = {s: returns_by_symbol[s] for s in kept_syms if s in returns_by_symbol}
        corr = correlation_matrix(rets)
        clusters = correlation_clusters(corr)
        for cluster in clusters:
            # Sort cluster members by current rank (already conviction-desc).
            ranked = [r for r in kept if r["symbol"] in cluster]
            if len(ranked) <= MAX_HIGH_CORRELATION_CLUSTER:
                continue
            keep_set = {r["symbol"] for r in ranked[:MAX_HIGH_CORRELATION_CLUSTER]}
            for r in ranked[MAX_HIGH_CORRELATION_CLUSTER:]:
                kept.remove(r)
                demoted.append({**r, "demotion_reason": "correlation_cluster"})

    # 4 — gross capital allocation with VOLATILITY-TARGETED sizing.
    # Each trade is sized to contribute ~`target_vol_pct` to the
    # portfolio's daily vol — equal risk contribution, not equal cash.
    # Falls back to ATR-stop sizing when realized vol is unavailable.
    weights: dict[str, float] = {}
    gross = 0.0
    target_trade_vol_pct = 1.0
    from app.services.risk_manager import annualised_volatility, vol_targeted_position_size
    for r in list(kept):
        entry = float(r.get("entry") or 0.0)
        sl = float(r.get("stoploss") or 0.0)
        sym = r.get("symbol")
        if entry <= 0 or sl <= 0 or entry == sl:
            continue
        # Vol-targeted sizing when returns history is available.
        ret_series = returns_by_symbol.get(sym) if returns_by_symbol else None
        if ret_series and len(ret_series) >= 20:
            vol_ann = annualised_volatility(ret_series)
            sized = vol_targeted_position_size(
                capital=capital, entry_price=entry, realized_vol_pct=vol_ann,
                target_vol_pct=target_trade_vol_pct, max_position_pct=5.0,
            )
            pos_pct = sized["target_capital_pct"]
        else:
            # Fallback: ATR-stop sizing — the prior default.
            risk_amount = capital * risk_per_trade_pct / 100.0
            pos_pct = risk_amount / abs(entry - sl) * entry / capital * 100.0
            pos_pct = min(pos_pct, 5.0)
        if gross + pos_pct > MAX_DIRECTIONAL_GROSS_PCT:
            kept.remove(r)
            demoted.append({**r, "demotion_reason": "gross_capital_cap"})
            continue
        gross += pos_pct
        sign = 1.0 if r.get("action") == "BUY" else -1.0
        weights[r["symbol"]] = sign * pos_pct / 100.0

    # 5 — VaR budget.
    var_pct = None
    if returns_by_symbol and weights:
        var_pct = parametric_var(weights, returns_by_symbol)
        # If parametric blows the budget, drop the lowest-conviction picks
        # until it's back inside.
        while var_pct is not None and var_pct > PORTFOLIO_VAR_BUDGET_PCT and kept:
            # Find the worst contributor — heuristically the smallest-conviction.
            worst = min(kept, key=lambda r: r.get("conviction", 0))
            kept.remove(worst)
            weights.pop(worst["symbol"], None)
            demoted.append({**worst, "demotion_reason": "var_budget"})
            var_pct = parametric_var(weights, returns_by_symbol) if weights else 0.0

    context = {
        "directional_count": len(kept),
        "gross_capital_pct": round(gross, 2),
        "portfolio_var_95_pct": var_pct,
        "var_method": "parametric",
        "high_corr_clusters": [sorted(c) for c in clusters],
        "caps": {
            "max_positions": MAX_DIRECTIONAL_POSITIONS,
            "max_per_sector": MAX_SAME_SECTOR,
            "max_per_cluster": MAX_HIGH_CORRELATION_CLUSTER,
            "max_gross_pct": MAX_DIRECTIONAL_GROSS_PCT,
            "var_budget_pct": PORTFOLIO_VAR_BUDGET_PCT,
        },
    }
    return {"kept": kept, "demoted": demoted, "portfolio_context": context}

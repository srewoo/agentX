"""Portfolio-level correlation + diversification analysis.

Lifts agentX from per-ticker recommendations to portfolio-aware ones:
before adding a new position, check how correlated its returns are to
the existing book. If it's > 0.7 with anything already open, the user
is doubling down on the same factor — flag it.

Outputs:
- ``correlation_to_open(candidate, positions)`` — single float, the
  *max* correlation to any open position (Pearson on daily log-returns
  over the last 60 trading days).
- ``correlation_matrix(symbols)`` — full N×N matrix for the diagnostics
  panel.
- ``concentration_summary(positions)`` — sector + factor concentration
  metrics surfaced in the dashboard.

This module is pure compute. It relies on the existing ``async_fetch_history``
to source price series; it does no I/O of its own beyond that.
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Window for the correlation calc. 60 trading days ≈ 3 calendar months —
# long enough to escape day-to-day noise, short enough to catch regime
# changes the user might still be in.
_CORRELATION_WINDOW_DAYS = 60


def _log_returns(closes: list[float]) -> list[float]:
    """Daily log-returns from a price series. Drops invalid rows."""
    out: list[float] = []
    for i in range(1, len(closes)):
        prev, curr = closes[i - 1], closes[i]
        if prev is None or curr is None or prev <= 0 or curr <= 0:
            continue
        out.append(math.log(curr / prev))
    return out


def pearson(a: list[float], b: list[float]) -> Optional[float]:
    """Pearson correlation coefficient. ``None`` for degenerate inputs."""
    if len(a) < 5 or len(a) != len(b):
        return None
    mean_a = sum(a) / len(a)
    mean_b = sum(b) / len(b)
    num = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    da = math.sqrt(sum((x - mean_a) ** 2 for x in a))
    db = math.sqrt(sum((y - mean_b) ** 2 for y in b))
    if da == 0 or db == 0:
        return None
    return num / (da * db)


async def _load_closes(symbol: str, *, days: int = _CORRELATION_WINDOW_DAYS) -> list[float]:
    """Pull last N closes via the existing data_fetcher. Empty list on failure."""
    try:
        from app.services.data_fetcher import async_fetch_history
        df = await async_fetch_history(symbol, period="3mo", interval="1d")
        if df is None or df.empty or "Close" not in df.columns:
            return []
        return [float(c) for c in df["Close"].tail(days + 1)]
    except Exception as e:
        logger.debug("portfolio_correlation: history fetch failed for %s: %s", symbol, e)
        return []


async def correlation_to_open(
    candidate_symbol: str,
    open_positions: list[dict[str, Any]],
) -> float:
    """Max abs correlation of ``candidate_symbol``'s returns to any open
    position. ``0.0`` when nothing's open or data is unavailable.

    Used by the risk-gate to surface a "high correlation" warning when
    the user is about to load up on a similar factor exposure.
    """
    if not open_positions:
        return 0.0
    open_symbols = sorted({
        str(p["symbol"]).upper() for p in open_positions if p.get("symbol")
    })
    if not open_symbols or candidate_symbol.upper() in open_symbols:
        # Already in the book — duplicate detection handled elsewhere.
        return 0.0

    # Fetch candidate + all open in parallel.
    series_list = await asyncio.gather(
        _load_closes(candidate_symbol),
        *[_load_closes(s) for s in open_symbols],
        return_exceptions=False,
    )
    cand_closes = series_list[0]
    if len(cand_closes) < 10:
        return 0.0
    cand_returns = _log_returns(cand_closes)

    best = 0.0
    for sym, other_closes in zip(open_symbols, series_list[1:]):
        if len(other_closes) < 10:
            continue
        other_returns = _log_returns(other_closes)
        n = min(len(cand_returns), len(other_returns))
        if n < 5:
            continue
        c = pearson(cand_returns[-n:], other_returns[-n:])
        if c is None:
            continue
        best = max(best, abs(c))
    return round(best, 4)


async def correlation_matrix(symbols: list[str]) -> list[list[Optional[float]]]:
    """Full N×N Pearson correlation matrix for a list of symbols.

    Cells are ``None`` when data is missing for one side. Diagonal is
    ``1.0``. Symmetric.
    """
    symbols = [s.upper() for s in symbols]
    series_list = await asyncio.gather(*[_load_closes(s) for s in symbols])
    returns: list[list[float]] = [_log_returns(s) if s else [] for s in series_list]

    n = len(symbols)
    matrix: list[list[Optional[float]]] = [[None] * n for _ in range(n)]
    for i in range(n):
        matrix[i][i] = 1.0
        for j in range(i + 1, n):
            a, b = returns[i], returns[j]
            if not a or not b:
                continue
            k = min(len(a), len(b))
            if k < 5:
                continue
            c = pearson(a[-k:], b[-k:])
            matrix[i][j] = matrix[j][i] = c
    return matrix


# ─────────────────────────────────────────────────────────────────────────
# Concentration metrics (synchronous — no I/O)
# ─────────────────────────────────────────────────────────────────────────

def concentration_summary(
    open_positions: list[dict[str, Any]],
    *,
    capital: float,
) -> dict[str, Any]:
    """Sector + position concentration breakdown for the dashboard.

    Returns:
        {
            "sector_pct": {"Energy": 12.5, "IT": 8.0, ...},
            "top_position_pct": float,
            "n_positions": int,
            "warnings": [str, ...],
        }
    """
    if capital <= 0:
        return {"sector_pct": {}, "top_position_pct": 0.0, "n_positions": 0, "warnings": []}

    sector_pct: dict[str, float] = {}
    pos_pcts: list[float] = []
    for p in open_positions:
        try:
            entry = float(p.get("entry_price", 0))
            shares = float(p.get("shares", 0))
        except (ValueError, TypeError):
            continue
        if entry <= 0 or shares <= 0:
            continue
        notional = entry * shares
        pct = notional / capital * 100.0
        pos_pcts.append(pct)
        sector = str(p.get("sector") or "Unknown")
        sector_pct[sector] = sector_pct.get(sector, 0.0) + pct

    sector_pct = {k: round(v, 2) for k, v in sector_pct.items()}
    top = max(pos_pcts) if pos_pcts else 0.0

    warnings: list[str] = []
    for sector, pct in sector_pct.items():
        if pct > 25.0:
            warnings.append(f"{sector} sector concentration {pct:.1f}% > 25% cap")
    if top > 5.0:
        warnings.append(f"largest position {top:.1f}% > 5% cap")

    return {
        "sector_pct": sector_pct,
        "top_position_pct": round(top, 2),
        "n_positions": len(pos_pcts),
        "warnings": warnings,
    }

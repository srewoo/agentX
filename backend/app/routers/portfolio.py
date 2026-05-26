"""Portfolio analytics endpoints (`/api/portfolio/*`).

Thin handlers — every line of business logic lives in
`app.services.portfolio`. Validation happens via Pydantic at the boundary.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.services import portfolio as svc
from app.utils import sanitize_symbol

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


# ── Request models ────────────────────────────────────────────
class CreateTransactionRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    side: Literal["BUY", "SELL"]
    qty: float = Field(gt=0)
    price: float = Field(ge=0)
    fees: float = Field(default=0.0, ge=0)
    notes: Optional[str] = Field(default=None, max_length=500)
    ts: Optional[str] = Field(default=None, max_length=40)

    @field_validator("symbol")
    @classmethod
    def _clean_symbol(cls, v: str) -> str:
        cleaned = sanitize_symbol(v)
        if not cleaned:
            raise ValueError("symbol must be a valid ticker")
        return cleaned


# ── Routes ────────────────────────────────────────────────────
@router.get("/summary")
async def get_summary(
    risk_free_rate: float = Query(svc.DEFAULT_RISK_FREE_RATE, ge=0.0, le=0.25),
):
    """Headline portfolio metrics.

    risk_free_rate is annualized (0.07 = 7%). Capped at 25% to catch
    accidental percent-vs-fraction mistakes.
    """
    await svc.ensure_schema()
    try:
        return await svc.build_summary(risk_free_rate=risk_free_rate)
    except Exception as e:
        logger.exception("portfolio.summary failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to build portfolio summary")


@router.get("/holdings")
async def get_holdings():
    """Open positions with live mark-to-market."""
    await svc.ensure_schema()
    try:
        txs = await svc.fetch_all_transactions_chronological()
        fifo = svc.compute_fifo(txs)
        positions = await svc.open_positions_with_marks(fifo.open_lots)
        return {"holdings": positions}
    except ValueError as e:
        # FIFO consistency violation — surface as 409 so ops sees it.
        logger.error("portfolio.holdings ledger error: %s", e)
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.exception("portfolio.holdings failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to load holdings")


@router.get("/transactions")
async def get_transactions(
    symbol: Optional[str] = Query(None, max_length=20),
    from_: Optional[str] = Query(None, alias="from", max_length=40),
    to: Optional[str] = Query(None, max_length=40),
    cursor: Optional[str] = Query(None, max_length=40),
    limit: int = Query(svc.DEFAULT_PAGE_LIMIT, ge=1, le=svc.MAX_PAGE_LIMIT),
):
    """Cursor-paginated transactions, newest first."""
    await svc.ensure_schema()
    sym = sanitize_symbol(symbol) if symbol else None
    try:
        return await svc.list_transactions(
            symbol=sym,
            from_ts=from_,
            to_ts=to,
            cursor=cursor,
            limit=limit,
        )
    except Exception as e:
        logger.exception("portfolio.transactions failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to list transactions")


@router.post("/transactions", status_code=201)
async def post_transaction(body: CreateTransactionRequest):
    """Record a manual buy/sell fill."""
    await svc.ensure_schema()
    try:
        row = await svc.insert_transaction(
            symbol=body.symbol,
            side=body.side,
            qty=body.qty,
            price=body.price,
            fees=body.fees,
            notes=body.notes,
            ts=body.ts,
        )
        return {"transaction": row}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("portfolio.create_transaction failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to record transaction")


@router.get("/equity-curve")
async def get_equity_curve(
    period: Literal["1m", "3m", "1y", "all"] = Query("all"),
):
    """Time series of cumulative realized equity for charting."""
    await svc.ensure_schema()
    try:
        return {"period": period, "points": await svc.equity_curve(period=period)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("portfolio.equity_curve failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to build equity curve")


@router.get("/sector-exposure")
async def get_sector_exposure():
    """% capital per sector with concentration flags."""
    await svc.ensure_schema()
    try:
        txs = await svc.fetch_all_transactions_chronological()
        fifo = svc.compute_fifo(txs)
        positions = await svc.open_positions_with_marks(fifo.open_lots)
        return {"sectors": await svc.sector_exposure(positions)}
    except Exception as e:
        logger.exception("portfolio.sector_exposure failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to compute sector exposure")


@router.get("/risk-dashboard")
async def get_risk_dashboard():
    """Portfolio-level risk heat: per-sector exposure + correlation matrix
    of open paper positions. Used by the popup's portfolio tab to
    visualise concentration and overlap at a glance.
    """
    import aiosqlite
    from app.database import DB_PATH
    try:
        from app.services.portfolio_correlation import correlation_to_open
    except Exception:
        correlation_to_open = None  # type: ignore[assignment]

    symbols: list[str] = []
    positions: list[dict] = []
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT symbol, direction, entry_price, shares, position_size, "
                "       stop_loss, target, status "
                "FROM paper_trades WHERE status='open'"
            ) as cur:
                async for r in cur:
                    pos = dict(r)
                    positions.append(pos)
                    if pos["symbol"] not in symbols:
                        symbols.append(pos["symbol"])
    except Exception as e:
        logger.exception("risk-dashboard: open positions query failed: %s", e)
        raise HTTPException(status_code=500, detail="failed to read positions")

    # Per-symbol pairwise correlation matrix.
    matrix: list[dict] = []
    if correlation_to_open is not None and len(symbols) >= 2:
        for sym in symbols:
            try:
                peers = [s for s in symbols if s != sym]
                c = await correlation_to_open(sym, peers)
                if isinstance(c, dict):
                    matrix.append({
                        "symbol": sym,
                        "max_correlation": c.get("max_correlation"),
                        "most_correlated_with": c.get("most_correlated_with"),
                    })
            except Exception:
                continue

    # Sector exposure for the *paper-trade* book (separate from portfolio
    # holdings — this is what the auto-trader is actually risking right now).
    paper_sector_buckets: dict[str, float] = {}
    total_size = sum(float(p.get("position_size") or 0.0) for p in positions) or 1.0
    for p in positions:
        sector = "Unknown"  # paper_trades has no sector column; this is best-effort
        size = float(p.get("position_size") or 0.0)
        paper_sector_buckets[sector] = paper_sector_buckets.get(sector, 0.0) + size
    paper_sector_exposure = [
        {"sector": k, "exposure_pct": round(v / total_size * 100.0, 1)}
        for k, v in paper_sector_buckets.items()
    ]
    paper_sector_exposure.sort(key=lambda r: r["exposure_pct"], reverse=True)

    return {
        "open_positions": positions,
        "correlation_matrix": matrix,
        "paper_sector_exposure": paper_sector_exposure,
        "alerts": _portfolio_alerts(positions, matrix, paper_sector_exposure),
    }


def _portfolio_alerts(
    positions: list[dict],
    matrix: list[dict],
    sector_exposure: list[dict],
) -> list[dict]:
    """Tag pile-ups, high correlation clusters, and sector over-exposure."""
    out: list[dict] = []
    # Highly-correlated cluster ≥ 0.7.
    for m in matrix:
        c = m.get("max_correlation")
        if isinstance(c, (int, float)) and c >= 0.7:
            out.append({
                "severity": "warn",
                "kind": "correlation",
                "message": (
                    f"{m['symbol']} is {c:.2f} correlated to "
                    f"{m.get('most_correlated_with')}"
                ),
            })
    # > 25% sector concentration.
    for s in sector_exposure:
        if s["exposure_pct"] > 25.0:
            out.append({
                "severity": "warn",
                "kind": "sector_concentration",
                "message": f"{s['sector']} = {s['exposure_pct']}% of paper book (cap 25%)",
            })
    return out

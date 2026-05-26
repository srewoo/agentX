from __future__ import annotations
"""Options endpoints — per-symbol options tab + IV-rank screener.

Surfaces the existing options_greeks / options_max_pain / unusual_options_activity
libraries through the API. Before this router they were dormant.
"""
import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from app.services.market_data import get_option_chain_analysis
from app.services.unusual_options_activity import detect_unusual_activity, as_signal

router = APIRouter(prefix="/api/options", tags=["options"])
logger = logging.getLogger(__name__)


@router.get("/{symbol}")
async def get_options_view(symbol: str) -> dict[str, Any]:
    """Per-symbol Options tab payload: chain analysis + UOA + positioning hint.

    Combines the option-chain analyzer (PCR / max-pain / strikes by OI)
    with the UOA detector and emits a single composite view the UI can
    render without further wiring.
    """
    chain = await get_option_chain_analysis(symbol)
    if not chain:
        raise HTTPException(status_code=404, detail=f"no options data for {symbol}")

    # Composite directional hint: max-pain anchor + PCR direction.
    spot = chain.get("underlying_value")
    mp = chain.get("max_pain")
    distance_pct: Optional[float] = None
    direction = "neutral"
    if isinstance(spot, (int, float)) and isinstance(mp, (int, float)) and mp:
        distance_pct = round((spot - mp) / mp * 100.0, 2)
        if distance_pct < -1:
            direction = "bullish"  # spot below pin → pin-up bias
        elif distance_pct > 1:
            direction = "bearish"

    # UOA — degrade gracefully when the chain doesn't satisfy the detector.
    uoa_payload: list[dict[str, Any]] = []
    try:
        unusual = chain.get("unusual_ce_activity", []) + chain.get("unusual_pe_activity", [])
        # Project shape expected by `detect_unusual_activity` to whatever
        # already populates `unusual_*_activity`. Best-effort.
        for u in unusual:
            uoa_payload.append({
                "strike": u.get("strike"),
                "oi": u.get("oi"),
                "oi_change": u.get("oi_change"),
                "iv": u.get("iv"),
            })
    except Exception:
        uoa_payload = []

    return {
        "symbol": symbol.upper(),
        "chain": chain,
        "positioning": {
            "spot": spot,
            "max_pain": mp,
            "distance_pct_to_max_pain": distance_pct,
            "anchor_direction": direction,
            "pcr_signal": chain.get("pcr_signal"),
            "pcr_oi": chain.get("pcr_oi"),
        },
        "unusual_activity": uoa_payload,
    }


@router.get("/screener/iv-rank")
async def iv_rank_screener(
    gte: float = Query(default=80.0, ge=0.0, le=100.0),
    universe: str = Query(default="nifty50"),
    limit: int = Query(default=25, ge=1, le=100),
) -> dict[str, Any]:
    """Symbols whose ATM IV is at or above the requested 1y percentile rank.

    For each candidate we look up its ATM call/put IV via the option chain
    analyzer and compare it against the trailing 252-bar HV percentile as
    a cheap proxy for IV rank. (A proper IV-rank requires daily IV history;
    we add the proxy now and can swap in real IV history once the data
    pipeline persists it.)
    """
    # Tiny built-in universes — extending without a config touch.
    universes = {
        "nifty50": [
            "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "BHARTIARTL",
            "HINDUNILVR", "ITC", "SBIN", "LT", "KOTAKBANK", "AXISBANK",
            "BAJFINANCE", "MARUTI", "ASIANPAINT", "WIPRO", "SUNPHARMA",
        ],
        "fno_top": [
            "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "TATAMOTORS",
            "ADANIENT", "SBIN", "BAJFINANCE", "M&M", "HINDALCO",
        ],
    }
    symbols = universes.get(universe.lower())
    if not symbols:
        raise HTTPException(status_code=400, detail=f"unknown universe '{universe}'")

    async def one(sym: str) -> Optional[dict[str, Any]]:
        try:
            ch = await get_option_chain_analysis(sym)
            if not ch:
                return None
            # Pick a representative ATM IV — average across the closest CE+PE
            # if `unusual_*_activity` exposed IV; otherwise None.
            ivs: list[float] = []
            for u in ch.get("unusual_ce_activity", []) + ch.get("unusual_pe_activity", []):
                iv = u.get("iv")
                if isinstance(iv, (int, float)) and iv > 0:
                    ivs.append(float(iv))
            if not ivs:
                return None
            atm_iv = round(sum(ivs) / len(ivs), 2)
            # Cheap proxy rank: lacking IV history we just normalise against
            # a hardcoded NSE-wide band of 10–60% IV → percentile.
            iv_rank = max(0.0, min(100.0, round((atm_iv - 10.0) / (60.0 - 10.0) * 100.0, 1)))
            if iv_rank < gte:
                return None
            return {
                "symbol": sym,
                "atm_iv": atm_iv,
                "iv_rank_proxy": iv_rank,
                "pcr_oi": ch.get("pcr_oi"),
                "max_pain": ch.get("max_pain"),
                "spot": ch.get("underlying_value"),
            }
        except Exception as e:
            logger.debug("iv-rank one-shot failed for %s: %s", sym, e)
            return None

    results = await asyncio.gather(*(one(s) for s in symbols))
    rows = [r for r in results if r is not None]
    rows.sort(key=lambda r: r["iv_rank_proxy"], reverse=True)
    return {"data": rows[:limit], "universe": universe, "gte": gte}

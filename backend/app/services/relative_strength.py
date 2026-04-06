from __future__ import annotations
"""
Relative Strength ranking — compares each stock's return to NIFTY 50 benchmark.

Professional fund managers prioritize stocks in the top RS percentile.
RS = stock 3-month return / NIFTY 3-month return.
RS rank = percentile among all tracked stocks.
"""
import asyncio
import logging
from datetime import timedelta
from typing import Any, Optional

from app.services.cache import cache_manager

logger = logging.getLogger(__name__)

_CACHE_TTL = timedelta(hours=6)
_NIFTY_SYMBOL = "^NSEI"


async def compute_relative_strength(
    symbols: list[str],
    period: str = "3mo",
) -> dict[str, Any]:
    """Compute RS ratio and rank for each symbol vs NIFTY 50.

    Returns:
        {
            "nifty_return": float,         # NIFTY 3-month return %
            "rankings": {
                "SYMBOL": {
                    "return_pct": float,
                    "rs_ratio": float,     # stock_return / nifty_return
                    "rs_rank": int,        # 0-100 percentile
                    "sector_rs": float,    # sector average RS ratio
                },
                ...
            }
        }
    """
    cache_key = f"relative_strength:{period}:{hash(tuple(sorted(symbols)))}"
    cached = await cache_manager.get(cache_key)
    if cached:
        return cached

    try:
        import yfinance as yf
        import pandas as pd

        loop = asyncio.get_event_loop()

        def _sync_compute() -> dict[str, Any]:
            # Fetch NIFTY benchmark
            nifty = yf.download(_NIFTY_SYMBOL, period=period, interval="1d", progress=False, auto_adjust=True)
            if nifty is None or nifty.empty:
                return {"nifty_return": None, "rankings": {}}

            nifty_start = float(nifty["Close"].dropna().iloc[0])
            nifty_end = float(nifty["Close"].dropna().iloc[-1])
            nifty_return = (nifty_end - nifty_start) / nifty_start * 100 if nifty_start > 0 else 0.0

            # Fetch all symbols
            returns: dict[str, float] = {}
            for sym in symbols:
                try:
                    ticker_sym = f"{sym}.NS"
                    df = yf.download(ticker_sym, period=period, interval="1d", progress=False, auto_adjust=True)
                    if df is None or df.empty:
                        continue
                    close = df["Close"].dropna()
                    if len(close) < 10:
                        continue
                    start_price = float(close.iloc[0])
                    end_price = float(close.iloc[-1])
                    if start_price > 0:
                        returns[sym] = (end_price - start_price) / start_price * 100
                except Exception:
                    continue

            if not returns:
                return {"nifty_return": nifty_return, "rankings": {}}

            # Compute RS ratios and percentile rank
            all_ratios = []
            for sym, ret in returns.items():
                ratio = ret / nifty_return if nifty_return and abs(nifty_return) > 0.1 else 1.0
                all_ratios.append((sym, ret, ratio))

            # Sort by RS ratio to compute percentile
            sorted_by_ratio = sorted(all_ratios, key=lambda x: x[2])
            n = len(sorted_by_ratio)

            rankings: dict[str, dict] = {}
            for rank_idx, (sym, ret, ratio) in enumerate(sorted_by_ratio):
                rs_rank = int((rank_idx / n) * 100) if n > 1 else 50
                rankings[sym] = {
                    "return_pct": round(ret, 2),
                    "rs_ratio": round(ratio, 3),
                    "rs_rank": rs_rank,
                }

            return {
                "nifty_return": round(nifty_return, 2),
                "rankings": rankings,
            }

        result = await asyncio.wait_for(
            loop.run_in_executor(None, _sync_compute),
            timeout=60.0,
        )
        if result and result.get("rankings"):
            await cache_manager.set(cache_key, result, ttl=_CACHE_TTL)
        return result or {"nifty_return": None, "rankings": {}}

    except Exception as e:
        logger.warning("Relative strength computation failed (non-critical): %s", e)
        return {"nifty_return": None, "rankings": {}}


def get_rs_strength_modifier(rs_rank: Optional[int]) -> int:
    """Return signal strength modifier based on RS rank percentile.

    Top 80th percentile → +1 (outperformer, tailwind)
    Bottom 20th percentile → -1 (underperformer, headwind)
    Otherwise → 0
    """
    if rs_rank is None:
        return 0
    if rs_rank >= 80:
        return 1
    if rs_rank <= 20:
        return -1
    return 0

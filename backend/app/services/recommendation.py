from __future__ import annotations
"""Indian-market multi-factor recommendation engine.

Composes existing services (technicals, FII/DII, relative strength, options
chain) into a weighted score → 0-100 conviction. ATR-based bands set
entry/SL/targets per horizon. Cached per (symbol, horizon).

Router wiring (parent agent should add to main.py):
    from app.routers import recommendations
    app.include_router(recommendations.router)

Factor scorers and ATR band math live in `recommendation_factors.py`
to keep this module under the 300-line ceiling.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.models.recommendation import (
    Action,
    FiiDiiSignal,
    FnoSignal,
    Horizon,
    MarketCapBand,
    Recommendation,
    SignalContribution,
)
from app.services.cache import cache_manager, make_cache_key
from app.services.data_fetcher import (
    MAJOR_STOCKS,
    async_fetch_history,
    get_delivery_volume,
    get_stock_quote,
)
from app.services.fii_dii import get_fii_dii_data
from app.services.market_data import get_option_chain_analysis
from app.services.recommendation_factors import (
    entry_sl_targets,
    fii_dii_score,
    fno_score,
    momentum_score,
    rs_score,
    trend_score,
    volatility_score,
    volume_delivery_score,
)
from app.services.relative_strength import compute_relative_strength
from app.services.technicals import compute_technicals

logger = logging.getLogger(__name__)

# Tunable factor weights — must sum to 1.0.
FACTOR_WEIGHTS: dict[str, float] = {
    "trend": 0.20,
    "momentum": 0.15,
    "volume_delivery": 0.15,
    "fno_oi": 0.15,
    "fii_dii": 0.10,
    "rel_strength": 0.10,
    "news_sentiment": 0.10,
    "volatility": 0.05,
}
assert abs(sum(FACTOR_WEIGHTS.values()) - 1.0) < 1e-9

_HORIZON_TO_DAYS: dict[Horizon, int] = {"intraday": 1, "swing": 10, "positional": 60}
_HORIZON_TO_PERIOD: dict[Horizon, str] = {"intraday": "1mo", "swing": "6mo", "positional": "1y"}
_HORIZON_TTL: dict[Horizon, timedelta] = {
    "intraday": timedelta(minutes=5),
    "swing": timedelta(hours=1),
    "positional": timedelta(days=1),
}

_PENNY_PRICE = 20.0
_MIN_AVG_VOLUME = 50_000
_BATCH_PARALLELISM = 5


def _classify_market_cap(symbol: str) -> MarketCapBand:
    # Why heuristic: avoid an extra yfinance .info call on the hot path.
    nifty50 = {s["symbol"] for s in MAJOR_STOCKS[:50]}
    universe = {s["symbol"] for s in MAJOR_STOCKS}
    if symbol in nifty50:
        return "LARGE"
    if symbol in universe:
        return "MID"
    return "SMALL"


def _sector_for(symbol: str) -> str:
    for s in MAJOR_STOCKS:
        if s["symbol"] == symbol:
            return s.get("sector", "N/A")
    return "N/A"


def default_universe(limit: int = 50) -> list[str]:
    return [s["symbol"] for s in MAJOR_STOCKS if not s["symbol"].startswith("^")][:limit]


def _build_reasons(
    contributions: list[SignalContribution],
    fno_sig: Optional[FnoSignal],
    fii_sig: Optional[FiiDiiSignal],
    delivery_pct: Optional[float],
) -> list[str]:
    out: list[str] = []
    for c in contributions:
        if c.score >= 0.4:
            out.append(f"{c.name.replace('_', ' ').title()} strongly bullish (score {c.score:+.2f}).")
        elif c.score <= -0.4:
            out.append(f"{c.name.replace('_', ' ').title()} strongly bearish (score {c.score:+.2f}).")
    if fno_sig:
        out.append(f"F&O activity: {fno_sig.replace('_', ' ').lower()}.")
    if fii_sig and fii_sig != "NEUTRAL":
        out.append(f"FII flow: {fii_sig.lower()}.")
    if delivery_pct is not None and delivery_pct >= 60:
        out.append(f"High delivery % ({delivery_pct:.1f}%) — institutional accumulation.")
    return out or ["Mixed signals — no strong conviction."]


def _avoid(
    symbol: str, horizon: Horizon, price: float, pchg_1d: float,
    delivery_pct: Optional[float], reason: str,
) -> Recommendation:
    p = max(0.01, price)
    return Recommendation(
        symbol=symbol, exchange="NSE", horizon=horizon, action="AVOID",
        conviction=0, entry=p, stoploss=max(0.01, p * 0.95),
        target1=max(0.01, p * 1.05), target2=None, risk_reward=0.0,
        timeframe_days=_HORIZON_TO_DAYS[horizon], signals=[],
        reasons=[reason], sector=_sector_for(symbol),
        market_cap_band=_classify_market_cap(symbol),
        last_price=p, price_change_pct_1d=round(pchg_1d, 2),
        delivery_pct=delivery_pct, fii_dii_signal=None, f_and_o_signal=None,
        generated_at=datetime.now(timezone.utc),
    )


def _score_all(
    tech: dict[str, Any], delivery_pct: Optional[float], fii_dii: dict[str, Any],
    options: Optional[dict[str, Any]], rs_rank: Optional[int], pchg_1d: float,
) -> tuple[list[SignalContribution], Optional[FnoSignal], Optional[FiiDiiSignal]]:
    s_t, v_t, d_t = trend_score(tech)
    s_m, v_m, d_m = momentum_score(tech)
    s_v, v_v, d_v = volume_delivery_score(tech, delivery_pct)
    s_f, v_f, d_f, fno_sig = fno_score(options, pchg_1d)
    s_fi, v_fi, d_fi, fii_sig = fii_dii_score(fii_dii)
    s_r, v_r, d_r = rs_score(rs_rank)
    s_atr, v_atr, d_atr = volatility_score(tech)
    contributions = [
        SignalContribution(name="trend", weight=FACTOR_WEIGHTS["trend"], value=v_t, score=s_t, direction=d_t),
        SignalContribution(name="momentum", weight=FACTOR_WEIGHTS["momentum"], value=v_m, score=s_m, direction=d_m),
        SignalContribution(name="volume_delivery", weight=FACTOR_WEIGHTS["volume_delivery"], value=v_v, score=s_v, direction=d_v),
        SignalContribution(name="fno_oi", weight=FACTOR_WEIGHTS["fno_oi"], value=v_f, score=s_f, direction=d_f),
        SignalContribution(name="fii_dii", weight=FACTOR_WEIGHTS["fii_dii"], value=v_fi, score=s_fi, direction=d_fi),
        SignalContribution(name="rel_strength", weight=FACTOR_WEIGHTS["rel_strength"], value=v_r, score=s_r, direction=d_r),
        SignalContribution(name="news_sentiment", weight=FACTOR_WEIGHTS["news_sentiment"], value=None, score=0.0, direction="neutral"),
        SignalContribution(name="volatility", weight=FACTOR_WEIGHTS["volatility"], value=v_atr, score=s_atr, direction=d_atr),
    ]
    return contributions, fno_sig, fii_sig


def conviction_from_score(weighted: float) -> int:
    """Map weighted score in [-1, 1] → conviction in [0, 100].

    Monotonic in |weighted| by construction. Exposed for tests.
    """
    return max(0, min(100, int(round(abs(weighted) * 100))))


def action_from_score(weighted: float) -> Action:
    if weighted > 0.15:
        return "BUY"
    if weighted < -0.15:
        return "SELL"
    return "HOLD"


async def generate_recommendation(
    symbol: str, horizon: Horizon = "swing"
) -> Optional[Recommendation]:
    """Build a single recommendation. Returns None on hard failure (no data).

    Cached per (symbol, horizon) with horizon-tuned TTL.
    """
    cache_key = make_cache_key("rec", symbol, horizon=horizon)
    cached = await cache_manager.get(cache_key)
    if cached:
        try:
            return Recommendation.model_validate(cached)
        except Exception:
            logger.debug("Stale cache shape for %s, regenerating", cache_key)

    df = await async_fetch_history(symbol, period=_HORIZON_TO_PERIOD[horizon], interval="1d")
    if df is None or df.empty or len(df) < 30:
        return None

    tech = compute_technicals(df)
    if not tech or not tech.get("current_price"):
        return None

    quote, delivery, fii_dii, options, rs = await asyncio.gather(
        get_stock_quote(symbol), get_delivery_volume(symbol),
        get_fii_dii_data(), get_option_chain_analysis(symbol),
        compute_relative_strength([symbol], period="3mo"),
        return_exceptions=True,
    )
    delivery = delivery if isinstance(delivery, dict) else {}
    fii_dii = fii_dii if isinstance(fii_dii, dict) else {}
    options = options if isinstance(options, dict) else None
    rs = rs if isinstance(rs, dict) else {}

    price = float(tech["current_price"])
    prev = tech.get("prev_price") or price
    pchg_1d = ((price - prev) / prev * 100) if prev else 0.0
    delivery_pct = delivery.get("delivery_pct")
    avg_vol = tech.get("volume_avg_20") or 0

    if price < _PENNY_PRICE or avg_vol < _MIN_AVG_VOLUME:
        return _avoid(
            symbol, horizon, price, pchg_1d, delivery_pct,
            "Below liquidity / price floor — too risky for a recommendation.",
        )

    rs_rank = (rs.get("rankings", {}) or {}).get(symbol, {}).get("rs_rank")

    contributions, fno_sig, fii_sig = _score_all(
        tech, delivery_pct, fii_dii, options, rs_rank, pchg_1d,
    )
    weighted = sum(c.score * c.weight for c in contributions)
    conviction = conviction_from_score(weighted)
    action = action_from_score(weighted)

    direction_up = weighted >= 0
    entry, sl, t1, t2 = entry_sl_targets(price, tech.get("atr"), horizon, direction_up)
    rr = abs(t1 - entry) / max(0.01, abs(entry - sl))

    rec = Recommendation(
        symbol=symbol, exchange="NSE", horizon=horizon, action=action,
        conviction=conviction, entry=round(entry, 2), stoploss=round(sl, 2),
        target1=round(t1, 2), target2=round(t2, 2) if t2 else None,
        risk_reward=round(rr, 2), timeframe_days=_HORIZON_TO_DAYS[horizon],
        signals=contributions,
        reasons=_build_reasons(contributions, fno_sig, fii_sig, delivery_pct),
        sector=_sector_for(symbol),
        market_cap_band=_classify_market_cap(symbol),
        last_price=round(price, 2),
        price_change_pct_1d=round(pchg_1d, 2),
        delivery_pct=delivery_pct, fii_dii_signal=fii_sig, f_and_o_signal=fno_sig,
        generated_at=datetime.now(timezone.utc),
    )
    await cache_manager.set(cache_key, rec.model_dump(mode="json"), ttl=_HORIZON_TTL[horizon])
    return rec


async def generate_batch(
    symbols: list[str], horizon: Horizon = "swing"
) -> tuple[list[Recommendation], list[dict]]:
    """Run `generate_recommendation` across symbols with bounded concurrency."""
    sem = asyncio.Semaphore(_BATCH_PARALLELISM)
    errors: list[dict] = []

    async def _one(sym: str) -> Optional[Recommendation]:
        async with sem:
            try:
                return await generate_recommendation(sym, horizon)
            except Exception as e:
                logger.exception("recommendation failed for %s", sym)
                errors.append({"symbol": sym, "error": str(e)})
                return None

    results = await asyncio.gather(*(_one(s) for s in symbols))
    return [r for r in results if r is not None], errors

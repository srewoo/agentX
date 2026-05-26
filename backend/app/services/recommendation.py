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
from app.services.fundamental_valuation import analyze_fundamental_valuation
from app.services.market_data import get_corporate_actions, get_option_chain_analysis
from app.services.recommendation_ensemble import build_recommendation_ensemble
from app.services.recommendation_factors import (
    entry_sl_targets,
    fii_dii_score,
    fno_score,
    options_positioning_score,
    fundamentals_score,
    momentum_score,
    news_sentiment_score,
    rs_score,
    trend_score,
    volatility_score,
    volume_delivery_score,
    weekly_trend_score,
)
from app.services.relative_strength import compute_relative_strength
from app.services.technicals import compute_technicals

logger = logging.getLogger(__name__)

# Tunable factor weights — must sum to 1.0. Two profiles: "calm" market
# (default) emphasises trend + momentum; "risk_off" market (high VIX) tilts
# toward defensive factors (delivery, fundamentals, rel-strength) and
# halves momentum's say. The orchestrator picks a profile based on India
# VIX at scan time — see `_select_weights()`.
WEIGHTS_CALM: dict[str, float] = {
    "trend": 0.16,
    "momentum": 0.12,
    "volume_delivery": 0.12,
    "fno_oi": 0.06,              # halved — overlaps with new options_positioning
    "fii_dii": 0.08,
    "rel_strength": 0.08,
    "news_sentiment": 0.04,      # -0.02 → options
    "volatility": 0.04,
    "fundamentals": 0.10,
    "weekly_trend": 0.12,
    "options_positioning": 0.08, # NEW — max-pain + UOA + PCR composite
}
WEIGHTS_RISK_OFF: dict[str, float] = {
    "trend": 0.12,
    "momentum": 0.06,
    "volume_delivery": 0.16,
    "fno_oi": 0.06,              # halved — overlaps with new options_positioning
    "fii_dii": 0.12,
    "rel_strength": 0.12,
    "news_sentiment": 0.04,
    "volatility": 0.06,
    "fundamentals": 0.14,
    "weekly_trend": 0.08,
    "options_positioning": 0.04, # smaller weight in risk-off (less reliable in panic)
}
for _w in (WEIGHTS_CALM, WEIGHTS_RISK_OFF):
    assert abs(sum(_w.values()) - 1.0) < 1e-9, f"weights must sum to 1: {sum(_w.values())}"

# Public alias — anything that imported `FACTOR_WEIGHTS` keeps working.
FACTOR_WEIGHTS = WEIGHTS_CALM


def _market_regime(india_vix: Optional[float], weekly_tech: Optional[dict[str, Any]] = None) -> str:
    """Coarse regime label used for swing/positional calibration."""
    if india_vix is not None and india_vix > 18.0:
        return "risk_off"
    if weekly_tech:
        ma = weekly_tech.get("moving_averages") or {}
        price = weekly_tech.get("current_price")
        sma20 = ma.get("sma20")
        adx = weekly_tech.get("adx")
        if price and sma20 and adx and adx >= 25:
            return "trend_up" if price > sma20 else "trend_down"
    return "neutral"


def _select_weights(india_vix: Optional[float], regime: Optional[str] = None) -> dict[str, float]:
    """Prefer learned weights from `recommendation_tuner` when available;
    fall back to hardcoded priors otherwise.

    The tuner persists per-regime weights once ≥200 resolved trades exist.
    Until then we use the original priors so the system has a sensible
    starting state.
    """
    is_risk_off = regime == "risk_off" or (india_vix is not None and india_vix > 18.0)
    regime_key = "risk_off" if is_risk_off else "calm"
    base: Optional[dict[str, float]] = None
    try:
        from app.services.recommendation_tuner import get_learned_weights
        learned = get_learned_weights(regime_key) or get_learned_weights("all")
        if learned and set(learned) == set(WEIGHTS_CALM):
            base = learned
    except Exception as e:  # tuner is best-effort; never break recs over this
        logger.debug("learned-weights lookup failed: %s", e)
    if base is None:
        base = WEIGHTS_RISK_OFF if is_risk_off else WEIGHTS_CALM

    # Apply 4-state regime bias (ADR-5: per-regime factor mix). The bias
    # multiplies factor weights, then we renormalise so the vector sums to
    # 1.0 — preserves the contract for downstream scorers.
    if regime in {"trend_up", "trend_down", "range_bound", "panic"}:
        try:
            from app.services.market_regime import factor_bias_for_regime
            bias = factor_bias_for_regime(regime)
            if bias:
                tilted = {k: max(0.0, v * bias.get(k, 1.0)) for k, v in base.items()}
                total = sum(tilted.values()) or 1.0
                base = {k: v / total for k, v in tilted.items()}
        except Exception as e:
            logger.debug("regime-bias application skipped: %s", e)
    return base

_HORIZON_TO_DAYS: dict[Horizon, int] = {"intraday": 1, "swing": 10, "positional": 60}
_HORIZON_TO_PERIOD: dict[Horizon, str] = {"intraday": "5d", "swing": "6mo", "positional": "1y"}
_HORIZON_TO_INTERVAL: dict[Horizon, str] = {"intraday": "5m", "swing": "1d", "positional": "1d"}
_HORIZON_TTL: dict[Horizon, timedelta] = {
    "intraday": timedelta(minutes=2),
    "swing": timedelta(hours=1),
    "positional": timedelta(days=1),
}

_PENNY_PRICE = 20.0
_MIN_AVG_VOLUME_BY_HORIZON: dict[Horizon, int] = {
    "intraday": 10_000,      # average 5-minute volume
    "swing": 50_000,         # average daily volume
    "positional": 50_000,    # average daily volume
}
# Bumped from 5 → 15: NSE/yfinance handle this comfortably and the previous
# value made cold batches sequential at 5-symbols-per-tick = 60s+ for 100
# symbols. Per-symbol `wait_for(20s)` still bounds the worst-case tail.
_BATCH_PARALLELISM = 15
_PER_SYMBOL_TIMEOUT_S = 12.0


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
        regime="blocked",
        weighted_score=0.0,
        factor_agreement=0.0,
        calibration_note="No directional signal: liquidity, event, or data-quality gate blocked this setup.",
        data_quality="limited",
        portfolio_context=None,
        generated_at=datetime.now(timezone.utc),
    )


def _score_all(
    tech: dict[str, Any], delivery_pct: Optional[float], fii_dii: dict[str, Any],
    options: Optional[dict[str, Any]], rs_rank: Optional[int], pchg_1d: float,
    news: Optional[list[dict[str, Any]]] = None,
    fundamentals: Optional[dict[str, Any]] = None,
    weekly_tech: Optional[dict[str, Any]] = None,
    weights: Optional[dict[str, float]] = None,
    use_learned_edge: bool = True,
    regime: Optional[str] = None,
    sector: Optional[str] = None,
    horizon: Optional[Horizon] = None,
    announcements: Optional[list[dict[str, Any]]] = None,
) -> tuple[list[SignalContribution], Optional[FnoSignal], Optional[FiiDiiSignal]]:
    w = weights or FACTOR_WEIGHTS
    s_t, v_t, d_t = trend_score(tech)
    s_m, v_m, d_m = momentum_score(tech)
    s_v, v_v, d_v = volume_delivery_score(tech, delivery_pct)
    s_f, v_f, d_f, fno_sig = fno_score(options, pchg_1d)
    # Options-positioning composite (max-pain distance + PCR + UOA z-score).
    # Pulls max_pain / current_price / UOA fields from the `options` dict if
    # the caller has populated them. Gracefully degrades when absent.
    _max_pain = (options or {}).get("max_pain") if isinstance(options, dict) else None
    _current_price = (options or {}).get("underlying_price") if isinstance(options, dict) else None
    _uoa_dir = (options or {}).get("uoa_direction") if isinstance(options, dict) else None
    _uoa_z = (options or {}).get("uoa_z") if isinstance(options, dict) else None
    s_op, v_op, d_op = options_positioning_score(
        options,
        current_price=_current_price,
        max_pain=_max_pain,
        uoa_direction=_uoa_dir,
        uoa_z=_uoa_z,
    )
    s_fi, v_fi, d_fi, fii_sig = fii_dii_score(fii_dii)
    s_r, v_r, d_r = rs_score(rs_rank)
    s_atr, v_atr, d_atr = volatility_score(tech)
    s_news, v_news, d_news = news_sentiment_score(news or [], announcements or [])
    s_fund, v_fund, d_fund = fundamentals_score(fundamentals)
    s_wk, v_wk, d_wk = weekly_trend_score(weekly_tech)
    # Self-improvement: scale each base weight by its learned edge from
    # `recommendation_tracker._factor_edge_cache`. Returns 1.0 when the
    # tracker has insufficient data, so this is a no-op for fresh deploys.
    from app.services.recommendation_tracker import factor_edge_multiplier
    def _w(name: str) -> float:
        base = w[name]
        adj = base * (
            factor_edge_multiplier(
                name, regime=regime, sector=sector, horizon=horizon,
            )
            if use_learned_edge else 1.0
        )
        # Pydantic guards: weight must stay in [0,1]. The multiplier can push
        # base × 1.5 above 1.0 only for factors whose base is already > 0.66
        # (which we don't have today), but clamp defensively anyway.
        return max(0.0, min(1.0, adj))
    contributions = [
        SignalContribution(name="trend", weight=_w("trend"), value=v_t, score=s_t, direction=d_t),
        SignalContribution(name="momentum", weight=_w("momentum"), value=v_m, score=s_m, direction=d_m),
        SignalContribution(name="volume_delivery", weight=_w("volume_delivery"), value=v_v, score=s_v, direction=d_v),
        SignalContribution(name="fno_oi", weight=_w("fno_oi"), value=v_f, score=s_f, direction=d_f),
        SignalContribution(name="fii_dii", weight=_w("fii_dii"), value=v_fi, score=s_fi, direction=d_fi),
        SignalContribution(name="rel_strength", weight=_w("rel_strength"), value=v_r, score=s_r, direction=d_r),
        SignalContribution(name="news_sentiment", weight=_w("news_sentiment"), value=v_news, score=s_news, direction=d_news),
        SignalContribution(name="volatility", weight=_w("volatility"), value=v_atr, score=s_atr, direction=d_atr),
        SignalContribution(name="fundamentals", weight=_w("fundamentals"), value=v_fund, score=s_fund, direction=d_fund),
        SignalContribution(name="weekly_trend", weight=_w("weekly_trend"), value=v_wk, score=s_wk, direction=d_wk),
        SignalContribution(name="options_positioning", weight=_w("options_positioning"), value=v_op, score=s_op, direction=d_op),
    ]
    return contributions, fno_sig, fii_sig


def _within_earnings_blackout(
    symbol: str, actions: list[dict], days: int = 5
) -> bool:
    """True when an "Earnings"/"Results" corporate action lands within
    `days` calendar days. Saves swing/positional from earnings gap risk.

    `actions` is the list returned by `get_corporate_actions()`. We match
    case-insensitively on `symbol` and the action_type contains "result"
    or "earning" (NSE labels them as "Quarterly Results" etc.).
    """
    if not actions:
        return False
    sym_u = symbol.upper()
    today = datetime.now(timezone.utc).date()
    horizon_end = today + timedelta(days=days)
    for a in actions:
        a_sym = (a.get("symbol") or "").upper()
        if a_sym != sym_u:
            continue
        action_type = (a.get("action_type") or a.get("subject") or "").lower()
        if "result" not in action_type and "earning" not in action_type:
            continue
        ex_str = a.get("ex_date") or a.get("date") or a.get("recordDate")
        if not ex_str:
            continue
        try:
            ex_date = datetime.fromisoformat(ex_str[:10]).date()
        except Exception:
            continue
        if today <= ex_date <= horizon_end:
            return True
    return False


def conviction_from_score(weighted: float) -> int:
    """Map weighted score in [-1, 1] → conviction in [0, 100].

    Monotonic in |weighted| by construction. Exposed for tests.
    """
    return max(0, min(100, int(round(abs(weighted) * 100))))


def calibrated_conviction(
    weighted: float,
    contributions: list[SignalContribution],
    *,
    risk_reward: float,
    regime: str,
) -> tuple[int, float, str]:
    """Convert score to conviction with agreement/regime/risk calibration."""
    base = conviction_from_score(weighted)
    direction = 1 if weighted >= 0 else -1
    directional = [c for c in contributions if abs(c.score) >= 0.15]
    if directional:
        aligned = sum(1 for c in directional if c.score * direction > 0)
        agreement = aligned / len(directional)
    else:
        agreement = 0.0

    multiplier = 0.75 + 0.35 * agreement
    if risk_reward < 1.5:
        multiplier *= 0.85
    elif risk_reward >= 2.0:
        multiplier *= 1.05
    if regime == "risk_off":
        multiplier *= 0.90
    elif regime in {"trend_up", "trend_down"}:
        multiplier *= 1.05

    # After a regime transition, widen all conviction thresholds by 25%
    # for 5 sessions (ADR-5). Implemented as: divide computed conviction by
    # the widening multiplier — fewer setups qualify until the new regime
    # settles. Falls back to 1.0 when no recent transition.
    try:
        from app.services.market_regime import get_recent_transition_multiplier
        widening = get_recent_transition_multiplier()
        if widening and widening > 1.0:
            multiplier = multiplier / widening
    except Exception:
        pass

    conviction = max(0, min(100, int(round(base * multiplier))))
    note = (
        f"Calibrated from raw score {base}/100 using "
        f"{agreement:.0%} factor agreement, R:R {risk_reward:.2f}, regime {regime}."
    )
    return conviction, round(agreement, 3), note


def action_from_score(
    weighted: float,
    *,
    regime: str = "neutral",
    factor_agreement: Optional[float] = None,
    signal_count: int = 0,
    deterministic_consensus: Optional[str] = None,
    llm_verdict: Optional[str] = None,
) -> Action:
    """Map the multi-factor weighted score to a BUY/SELL/HOLD action.

    History: the pre-2026-05-26 thresholds (±0.15 / ±0.20 risk-off) were
    so timid that strength-10 deterministic signals routinely produced
    HOLD recs because cross-factor disagreement diluted the score. The
    ``recommendation_outcomes`` tracker accumulated **1 row in months** —
    no end-to-end accuracy signal at all.

    New conviction rules:
    1. **Lower base thresholds** (±0.10 / ±0.15 risk-off). Less timid.
    2. **High-agreement bypass**: when ≥ 3 deterministic signals point
       the same way *and* factor_agreement ≥ 0.7 *and* LLM judge didn't
       drop, halve the threshold — the multi-factor stack is allowed to
       trust the rule layer's conviction.
    3. **Regime-adaptive thresholds**: in strong trends, the
       in-trend-direction threshold is relaxed; counter-trend stays
       firm.
    """
    # ── Base thresholds (lower than the old ±0.15) ─────────────────────
    if regime == "risk_off":
        pos_threshold = 0.15
        neg_threshold = 0.15
    elif regime == "trend_up":
        pos_threshold = 0.08   # easier to take BUY in confirmed uptrend
        neg_threshold = 0.15
    elif regime == "trend_down":
        pos_threshold = 0.15
        neg_threshold = 0.08
    else:
        pos_threshold = 0.10
        neg_threshold = 0.10

    # ── High-agreement bypass ──────────────────────────────────────────
    strong_consensus = (
        signal_count >= 3
        and (factor_agreement or 0) >= 0.7
        and llm_verdict != "drop"
        and deterministic_consensus in (None, "bullish", "bearish")
    )
    if strong_consensus:
        # The deterministic stack agrees; halve the gate so a borderline
        # weighted score still passes.
        pos_threshold *= 0.5
        neg_threshold *= 0.5

    if weighted > pos_threshold:
        return "BUY"
    if weighted < -neg_threshold:
        return "SELL"
    return "HOLD"


async def generate_recommendation(
    symbol: str,
    horizon: Horizon = "swing",
    *,
    fii_dii_ctx: Optional[dict[str, Any]] = None,
    corp_actions_ctx: Optional[list[dict]] = None,
    rs_ctx: Optional[dict[str, Any]] = None,
    use_llm_judge: bool = False,
    reasoning_effort: str = "medium",
) -> Optional[Recommendation]:
    """Build a single recommendation. Returns None on hard failure (no data).

    Cached per (symbol, horizon) with horizon-tuned TTL.

    `fii_dii_ctx` / `corp_actions_ctx` are optional pre-fetched globals —
    `generate_batch` passes them in once so we don't hammer NSE 100×.
    """
    cache_key = make_cache_key("rec", symbol, horizon=horizon, llm_judge=1 if use_llm_judge else 0)
    cached = await cache_manager.get(cache_key)
    if cached:
        try:
            return Recommendation.model_validate(cached)
        except Exception:
            logger.debug("Stale cache shape for %s, regenerating", cache_key)

    df = await async_fetch_history(
        symbol,
        period=_HORIZON_TO_PERIOD[horizon],
        interval=_HORIZON_TO_INTERVAL[horizon],
    )
    if df is None or df.empty or len(df) < 30:
        return None

    tech = compute_technicals(df)
    if not tech or not tech.get("current_price"):
        return None

    # Higher-timeframe (weekly) confluence — only meaningful for swing /
    # positional. Resample the same daily df we already have so we don't
    # pay an extra fetch.
    weekly_tech: Optional[dict[str, Any]] = None
    if horizon != "intraday":
        try:
            wdf = df.resample("W").agg({
                "Open": "first", "High": "max", "Low": "min",
                "Close": "last", "Volume": "sum",
            }).dropna()
            if len(wdf) >= 12:
                weekly_tech = compute_technicals(wdf)
        except Exception:
            weekly_tech = None

    # Cross-cutting fetches in parallel. News + fundamentals + corp_actions
    # + market regime are best-effort: failures degrade the depth but don't
    # abort the recommendation.
    #
    # Fundamentals is cache-only here — `get_fundamentals` does a screener.in
    # scrape on miss which is O(seconds), and we run this for ~100 symbols
    # per batch. The Search page (single symbol) does the cold fetch and
    # populates the cache; the batch path then reuses it.
    from app.services.market_data import get_corporate_actions
    from app.services.sentiment import get_stock_news

    fund_cache_key = make_cache_key("stock:fundamentals", symbol)
    fund_last_good_key = make_cache_key("stock:fundamentals:lastgood", symbol)

    async def _cached_fundamentals() -> Optional[dict[str, Any]]:
        f = await cache_manager.get(fund_cache_key)
        if f:
            return f
        return await cache_manager.get(fund_last_good_key)

    # Per-symbol fetches only — globals are passed via *_ctx (or fetched
    # once below for the single-symbol path).
    if fii_dii_ctx is None:
        fii_dii_task = get_fii_dii_data()
    else:
        async def _from_ctx(): return fii_dii_ctx
        fii_dii_task = _from_ctx()

    if corp_actions_ctx is None:
        corp_task = get_corporate_actions()
    else:
        async def _ca_ctx(): return corp_actions_ctx
        corp_task = _ca_ctx()

    # Relative strength: when batch hoists `rs_ctx`, look up cheaply by
    # symbol; otherwise fall back to a single-symbol fetch.
    if rs_ctx is not None:
        async def _rs_from_ctx(): return rs_ctx
        rs_task = _rs_from_ctx()
    else:
        rs_task = compute_relative_strength([symbol], period="3mo")

    quote, delivery, fii_dii, options, rs, news, fundamentals, corp_actions = await asyncio.gather(
        get_stock_quote(symbol), get_delivery_volume(symbol),
        fii_dii_task, get_option_chain_analysis(symbol),
        rs_task,
        get_stock_news(symbol, limit=8),
        _cached_fundamentals(),
        corp_task,
        return_exceptions=True,
    )
    delivery = delivery if isinstance(delivery, dict) else {}
    fii_dii = fii_dii if isinstance(fii_dii, dict) else {}
    options = options if isinstance(options, dict) else None
    rs = rs if isinstance(rs, dict) else {}
    news = news if isinstance(news, list) else []
    fundamentals = fundamentals if isinstance(fundamentals, dict) else None
    corp_actions = corp_actions if isinstance(corp_actions, list) else []

    price = float(tech["current_price"])
    prev = tech.get("prev_price") or price
    pchg_1d = ((price - prev) / prev * 100) if prev else 0.0
    delivery_pct = delivery.get("delivery_pct")
    avg_vol = tech.get("volume_avg_20") or 0

    if price < _PENNY_PRICE or avg_vol < _MIN_AVG_VOLUME_BY_HORIZON[horizon]:
        return _avoid(
            symbol, horizon, price, pchg_1d, delivery_pct,
            "Below liquidity / price floor — too risky for a recommendation.",
        )

    # Earnings blackout — swing/positional callers don't want signals fired
    # 5 days ahead of a results announcement (gap-risk eats SL).
    if horizon != "intraday" and _within_earnings_blackout(symbol, corp_actions, days=5):
        return _avoid(
            symbol, horizon, price, pchg_1d, delivery_pct,
            "Earnings within 5 trading days — gap risk too high for a swing call.",
        )

    rs_rank = (rs.get("rankings", {}) or {}).get(symbol, {}).get("rs_rank")

    india_vix = (fii_dii or {}).get("india_vix") if isinstance(fii_dii, dict) else None
    regime = _market_regime(india_vix, weekly_tech)
    weights = _select_weights(india_vix, regime)
    sector = _sector_for(symbol)

    fundamental_valuation = analyze_fundamental_valuation(fundamentals, sector=sector)
    if fundamentals is not None:
        fundamentals = {**fundamentals, "fundamental_valuation": fundamental_valuation}

    # Deep fundamentals (cash flow + balance sheet + earnings quality + moat)
    # is best-effort and cached separately; tucked under `deep_fundamentals`
    # for the factor scorer to pick up. Skipped for intraday — quality
    # matters far less for 1-day calls and we don't want yfinance latency
    # on the hot intraday path.
    if horizon != "intraday":
        try:
            deep_cache_key = make_cache_key("stock:deep_fundamentals", symbol)
            deep = await cache_manager.get(deep_cache_key)
            if not deep:
                from app.services.fundamentals_deep import get_deep_fundamentals
                deep = await asyncio.wait_for(get_deep_fundamentals(symbol), timeout=12)
                # Long TTL — fundamentals change quarterly.
                await cache_manager.set(deep_cache_key, deep, ttl=timedelta(days=7))
                # Persist a point-in-time snapshot. Backtests should load
                # the snapshot dated ≤ entry bar instead of fetching live
                # data — yfinance returns restated financials, which leak.
                try:
                    from app.services.execution_costs import snapshot_fundamentals
                    await snapshot_fundamentals(
                        symbol, deep, source="yfinance_deep",
                        composite_score=deep.get("composite_score"),
                    )
                except Exception:
                    pass
            if isinstance(deep, dict) and deep.get("composite_score", 0) > 0:
                fundamentals = {**(fundamentals or {}), "deep_fundamentals": deep}
        except Exception as e:
            logger.debug("deep fundamentals skipped for %s: %s", symbol, e)

    # Filter announcements to this symbol so NSE-wide noise doesn't leak in.
    sym_u = symbol.upper()
    symbol_announcements = [
        a for a in (corp_actions or [])
        if (a.get("symbol") or "").upper() == sym_u
    ]
    contributions, fno_sig, fii_sig = _score_all(
        tech, delivery_pct, fii_dii, options, rs_rank, pchg_1d,
        news=news, fundamentals=fundamentals, weekly_tech=weekly_tech,
        weights=weights, regime=regime, sector=sector, horizon=horizon,
        announcements=symbol_announcements,
    )
    weighted = sum(c.score * c.weight for c in contributions)

    direction_up = weighted >= 0
    entry, sl, t1, t2 = entry_sl_targets(price, tech.get("atr"), horizon, direction_up)
    rr = abs(t1 - entry) / max(0.01, abs(entry - sl))
    conviction, agreement, calibration_note = calibrated_conviction(
        weighted, contributions, risk_reward=rr, regime=regime,
    )
    # Count directional contributors and pick majority direction so the
    # conviction bypass can fire when the deterministic stack aligns.
    directional_count = 0
    bullish_count = 0
    bearish_count = 0
    for c in contributions:
        if not isinstance(c, dict):
            continue
        if c.get("direction") in ("bullish", "bearish"):
            directional_count += 1
            if c["direction"] == "bullish":
                bullish_count += 1
            else:
                bearish_count += 1
    if bullish_count > bearish_count:
        det_consensus: Optional[str] = "bullish"
    elif bearish_count > bullish_count:
        det_consensus = "bearish"
    else:
        det_consensus = None

    action = action_from_score(
        weighted,
        regime=regime,
        factor_agreement=agreement,
        signal_count=directional_count,
        deterministic_consensus=det_consensus,
        # We don't have the LLM judge verdict at this layer (it acts on
        # individual signals, not the aggregated rec), so leave None
        # — the bypass requires this NOT be "drop".
        llm_verdict=None,
    )
    # Lowered the post-call conviction-cap from 45 → 35: the new thresholds
    # already gate sub-meaningful scores, so the secondary cap was a
    # belt-and-braces filter that killed too many good signals.
    if action in ("BUY", "SELL") and conviction < 35:
        action = "HOLD"
        calibration_note += " Directional call demoted to HOLD because calibrated conviction is below 35."

    portfolio_context = None
    if action in ("BUY", "SELL"):
        try:
            from app.services.portfolio import portfolio_recommendation_context
            portfolio_context = await portfolio_recommendation_context(
                symbol=symbol,
                sector=sector,
                action=action,
            )
            adjustment = int(portfolio_context.get("action_adjustment") or 0)
            if adjustment:
                conviction = max(0, min(100, conviction + adjustment))
                calibration_note += f" Portfolio adjustment applied ({adjustment:+d} conviction)."
            for note in portfolio_context.get("notes") or []:
                calibration_note += f" {note}"
            if action == "BUY" and portfolio_context.get("decision") == "block_add":
                action = "HOLD"
                calibration_note += " BUY demoted to HOLD because portfolio concentration is already high."
        except Exception as e:
            logger.debug("portfolio context skipped for %s: %s", symbol, e)

    data_quality = "eod_verified"
    if horizon == "intraday":
        data_quality = "delayed_intraday"
    elif len(df) < 120:
        data_quality = "limited_history"

    ensemble = build_recommendation_ensemble(
        action=action,
        weighted_score=weighted,
        calibrated_conviction=conviction,
        factor_agreement=agreement,
        risk_reward=rr,
        regime=regime,
        contributions=contributions,
        fundamental_valuation=fundamental_valuation,
        portfolio_context=portfolio_context,
        data_quality=data_quality,
    )
    if action in {"BUY", "SELL", "HOLD"}:
        action = ensemble["suggested_action"]
        conviction = ensemble["final_conviction"]

    rec = Recommendation(
        symbol=symbol, exchange="NSE", horizon=horizon, action=action,
        conviction=conviction, entry=round(entry, 2), stoploss=round(sl, 2),
        target1=round(t1, 2), target2=round(t2, 2) if t2 else None,
        risk_reward=round(rr, 2), timeframe_days=_HORIZON_TO_DAYS[horizon],
        signals=contributions,
        reasons=_build_reasons(contributions, fno_sig, fii_sig, delivery_pct),
        sector=sector,
        market_cap_band=_classify_market_cap(symbol),
        last_price=round(price, 2),
        price_change_pct_1d=round(pchg_1d, 2),
        delivery_pct=delivery_pct, fii_dii_signal=fii_sig, f_and_o_signal=fno_sig,
        regime=regime,
        weighted_score=round(weighted, 4),
        factor_agreement=agreement,
        calibration_note=calibration_note,
        data_quality=data_quality,
        portfolio_context=portfolio_context,
        fundamental_valuation=fundamental_valuation,
        ensemble=ensemble,
        llm_judge=None,
        generated_at=datetime.now(timezone.utc),
    )

    if use_llm_judge and horizon != "intraday" and action != "AVOID":
        try:
            from app.services.recommendation_llm_judge import judge_recommendation
            judge = await judge_recommendation(
                rec,
                evidence={
                    "fundamental_valuation": fundamental_valuation,
                    "market_regime": regime,
                    "factor_agreement": agreement,
                    "risk_reward": rr,
                    "data_quality": data_quality,
                },
                reasoning_effort=reasoning_effort,
            )
            ensemble = build_recommendation_ensemble(
                action=action,
                weighted_score=weighted,
                calibrated_conviction=conviction,
                factor_agreement=agreement,
                risk_reward=rr,
                regime=regime,
                contributions=contributions,
                fundamental_valuation=fundamental_valuation,
                portfolio_context=portfolio_context,
                data_quality=data_quality,
                llm_judge=judge,
            )
            rec = rec.model_copy(update={
                "action": ensemble["suggested_action"],
                "conviction": ensemble["final_conviction"],
                "ensemble": ensemble,
                "llm_judge": judge,
                "calibration_note": f"{calibration_note} LLM judge: {judge.get('summary', '')}".strip(),
            })
        except Exception as e:
            logger.debug("llm judge skipped for %s: %s", symbol, e)

    # Meta-labeling: gate conviction by secondary classifier's p(win).
    # No-op when the model hasn't been trained yet (returns None).
    try:
        from app.services.ml_meta_label import meta_label_probability
        rec_payload = {
            "action": rec.action,
            "signals": rec.signals,
            "weighted_score": rec.weighted_score,
            "factor_agreement": rec.factor_agreement,
            "conviction": rec.conviction,
            "entry": rec.entry, "stoploss": rec.stoploss, "target1": rec.target1,
            "regime": rec.regime,
        }
        p_meta = meta_label_probability(rec_payload)
        if p_meta is not None and rec.action in ("BUY", "SELL"):
            # Conviction × p_meta with a 0.5 floor (don't fully veto on a
            # weak prior — let the multi-factor stack still speak). Below
            # p_meta=0.45 we demote to HOLD; above 0.55 we keep BUY/SELL.
            scaled = int(round(rec.conviction * max(0.5, min(1.5, p_meta * 2))))
            new_action = rec.action if p_meta >= 0.45 else "HOLD"
            rec = rec.model_copy(update={
                "conviction": max(0, min(100, scaled)),
                "action": new_action,
                "calibration_note": (
                    f"{rec.calibration_note} Meta-label p(win)={p_meta:.2f} → "
                    f"conviction {rec.conviction} → {scaled}."
                ),
            })
    except Exception as e:
        logger.debug("meta-label gating skipped for %s: %s", symbol, e)

    await cache_manager.set(cache_key, rec.model_dump(mode="json"), ttl=_HORIZON_TTL[horizon])
    # Persist to the outcome tracker (no-op for HOLD/AVOID). Fire-and-forget
    # — tracker failures must not break the recommendation surface.
    try:
        from app.services.recommendation_tracker import store_recommendation
        await store_recommendation(rec)
    except Exception as e:
        logger.debug("store_recommendation skipped for %s: %s", symbol, e)
    return rec


async def generate_batch(
    symbols: list[str], horizon: Horizon = "swing"
) -> tuple[list[Recommendation], list[dict]]:
    """Run `generate_recommendation` across symbols with bounded concurrency.

    Output is sector-diversified: at most `MAX_PER_SECTOR` recommendations
    of the same sector survive in the directional (BUY/SELL) bucket. The
    rest get demoted to HOLD with a "sector cap reached" reason. Avoids
    the "top 5 picks are all banks" failure mode.
    """
    sem = asyncio.Semaphore(_BATCH_PARALLELISM)
    errors: list[dict] = []

    # Hoist global fetches — these don't depend on symbol and would
    # otherwise hammer NSE / yfinance N×. One call per batch is enough.
    # Relative strength is also batch-friendly: one call, all rankings.
    fii_ctx_raw, corp_ctx_raw, rs_ctx_raw = await asyncio.gather(
        get_fii_dii_data(), get_corporate_actions(),
        compute_relative_strength(symbols, period="3mo"),
        return_exceptions=True,
    )
    fii_ctx: dict[str, Any] = fii_ctx_raw if isinstance(fii_ctx_raw, dict) else {}
    corp_ctx: list[dict] = corp_ctx_raw if isinstance(corp_ctx_raw, list) else []
    rs_ctx: dict[str, Any] = rs_ctx_raw if isinstance(rs_ctx_raw, dict) else {}

    async def _one(sym: str) -> Optional[Recommendation]:
        async with sem:
            try:
                # Hard cap per-symbol — one slow upstream shouldn't tank
                # the whole batch.
                return await asyncio.wait_for(
                    generate_recommendation(
                        sym, horizon,
                        fii_dii_ctx=fii_ctx, corp_actions_ctx=corp_ctx, rs_ctx=rs_ctx,
                    ),
                    timeout=_PER_SYMBOL_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                errors.append({"symbol": sym, "error": "timeout"})
                return None
            except Exception as e:
                logger.exception("recommendation failed for %s", sym)
                errors.append({"symbol": sym, "error": str(e)})
                return None

    results = await asyncio.gather(*(_one(s) for s in symbols))
    recs = [r for r in results if r is not None]
    # Cross-sectional rerank: re-score each rec's conviction with a
    # blended absolute + cross-sectional score so picks reflect
    # *relative* strength across the batch. Largest single documented
    # edge improvement on factor systems (+3–5pp win rate, Asness/
    # Stockopedia/Fama-French). Only meaningful when batch >= 10.
    try:
        recs = _apply_cross_sectional_rank(recs)
    except Exception as e:
        logger.debug("cross-sectional rerank skipped: %s", e)
    diversified = _diversify_by_sector(recs)
    # Portfolio-level risk pass: correlation, VaR, exposure budget.
    # Best-effort — failures fall back to sector-only diversification.
    try:
        diversified = await _apply_portfolio_risk(diversified)
    except Exception as e:
        logger.debug("portfolio risk pass skipped: %s", e)
    return diversified, errors


async def _apply_portfolio_risk(recs: list[Recommendation]) -> list[Recommendation]:
    """Enforce correlation + VaR + exposure caps across the basket.

    Pulls 60d daily history per symbol (cached by data_fetcher), builds
    a returns matrix, then defers to `portfolio_risk.enforce_exposure_budget`.
    Picks the engine wants to demote get their `action` flipped to HOLD
    and a reason appended.
    """
    from app.services.portfolio_risk import enforce_exposure_budget

    syms = [r.symbol for r in recs if r.action in ("BUY", "SELL")]
    if not syms:
        return recs

    # Fetch returns in parallel — cheap because data_fetcher caches.
    async def _ret(sym: str) -> tuple[str, list[float]]:
        try:
            df = await async_fetch_history(sym, period="3mo", interval="1d")
            if df is None or df.empty:
                return sym, []
            prices = df["Close"].dropna().tolist()
            from app.services.portfolio_risk import _pct_returns
            return sym, _pct_returns(prices)
        except Exception:
            return sym, []

    rets_pairs = await asyncio.gather(*(_ret(s) for s in syms))
    returns_by_symbol = {s: r for s, r in rets_pairs if r}

    plain = [r.model_dump() for r in recs]
    result = enforce_exposure_budget(plain, returns_by_symbol=returns_by_symbol)
    demoted_syms = {d["symbol"]: d.get("demotion_reason", "portfolio_cap") for d in result["demoted"]}
    ctx = result["portfolio_context"]

    out: list[Recommendation] = []
    for r in recs:
        if r.symbol in demoted_syms and r.action in ("BUY", "SELL"):
            reason = demoted_syms[r.symbol]
            r = r.model_copy(update={
                "action": "HOLD",
                "reasons": [*r.reasons, f"Portfolio risk cap ({reason}) — demoted to HOLD."],
                "portfolio_context": ctx,
            })
        else:
            r = r.model_copy(update={"portfolio_context": ctx})
        out.append(r)
    return out


def _apply_cross_sectional_rank(recs: list[Recommendation]) -> list[Recommendation]:
    """Re-score conviction with a blended absolute + cross-sectional score.

    Builds the {symbol: {factor: score}} matrix from each rec's
    `signals` list, calls `cross_sectional_rank`, then updates each
    rec's `weighted_score`, `action`, and `conviction`. Sector/risk
    diversification still runs downstream.
    """
    from app.services.cross_sectional import (
        cross_sectional_rank,
        blend_absolute_and_cross_sectional,
    )
    if len(recs) < 10:
        return recs

    factor_matrix: dict[str, dict[str, float]] = {}
    for r in recs:
        factor_matrix[r.symbol] = {c.name: float(c.score) for c in (r.signals or [])}
    # Use the first rec's actual weights — they're all the same regime.
    first_weights = {c.name: float(c.weight) for c in (recs[0].signals or [])}
    cs = cross_sectional_rank(factor_matrix, weights=first_weights)

    out: list[Recommendation] = []
    for r in recs:
        info = cs.get(r.symbol) or {}
        cs_score = float(info.get("cross_sectional_score") or 0.0)
        rank = info.get("rank")
        blended = blend_absolute_and_cross_sectional(
            r.weighted_score, cs_score, cs_weight=0.4,
        )
        # Re-derive action + conviction from the blended score. Pass the
        # signal-count / agreement context so the new high-agreement
        # bypass also applies to cross-sectional re-ranks (otherwise
        # cross-sectional re-grading could revert good calls to HOLD).
        new_conviction, agreement, _note = calibrated_conviction(
            blended, r.signals, risk_reward=r.risk_reward, regime=r.regime or "neutral",
        )
        # Count directional signals on the underlying contribution list.
        dir_count = sum(
            1 for s in (r.signals or [])
            if getattr(s, "direction", None) in ("bullish", "bearish")
        )
        new_action = action_from_score(
            blended,
            regime=r.regime or "neutral",
            factor_agreement=agreement,
            signal_count=dir_count,
        )
        out.append(r.model_copy(update={
            "weighted_score": round(blended, 4),
            "action": new_action,
            "conviction": new_conviction,
            "factor_agreement": agreement,
            "calibration_note": (
                f"{r.calibration_note} Cross-sectional rerank: rank {rank}/{len(recs)}, "
                f"cs_score {cs_score:+.3f} blended at 40% weight."
            ),
        }))
    return out


# Tunable: at most this many directional (BUY/SELL) picks per sector.
MAX_PER_SECTOR = 2


def _diversify_by_sector(recs: list[Recommendation]) -> list[Recommendation]:
    """Demote excess same-sector directional picks to HOLD.

    Directional ranking: highest conviction first, ties broken by R:R. Once
    a sector hits `MAX_PER_SECTOR` directional slots, further picks in that
    sector get a HOLD action and a "sector cap reached" reason appended.
    AVOID stays AVOID (already low-conviction). Order is preserved so
    downstream filters/sort still work.
    """
    if not recs:
        return recs

    # Build a stable sort: directional first by conviction desc, then R:R.
    directional = sorted(
        [r for r in recs if r.action in ("BUY", "SELL")],
        key=lambda r: (r.conviction, r.risk_reward),
        reverse=True,
    )
    sector_count: dict[str, int] = {}
    demoted: set[str] = set()
    for r in directional:
        sec = (r.sector or "N/A").lower()
        if sector_count.get(sec, 0) >= MAX_PER_SECTOR:
            demoted.add(r.symbol)
        else:
            sector_count[sec] = sector_count.get(sec, 0) + 1

    if not demoted:
        return recs

    out: list[Recommendation] = []
    for r in recs:
        if r.symbol in demoted:
            r = r.model_copy(update={
                "action": "HOLD",
                "reasons": [*r.reasons, f"Sector cap reached ({MAX_PER_SECTOR} per sector) — demoted to HOLD."],
            })
        out.append(r)
    return out

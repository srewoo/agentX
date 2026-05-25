from __future__ import annotations
"""Factor scorers + ATR band math for the recommendation engine.

Pure functions. No I/O. Each scorer returns a normalized score in [-1, +1]
plus a raw indicator value and direction, so the caller can build a
SignalContribution without re-deriving anything.
"""
from typing import Any, Optional

from app.models.recommendation import FiiDiiSignal, FnoSignal, Horizon


# (sl_mult, t1_mult, t2_mult) in ATR units — wider for longer horizons.
HORIZON_ATR_BANDS: dict[Horizon, tuple[float, float, float]] = {
    "intraday": (1.0, 1.5, 2.5),
    "swing": (1.5, 3.0, 5.0),
    "positional": (2.5, 5.0, 9.0),
}


def clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _direction(score: float) -> str:
    if score > 0.05:
        return "bullish"
    if score < -0.05:
        return "bearish"
    return "neutral"


def trend_score(tech: dict[str, Any]) -> tuple[float, Optional[float], str]:
    ma = tech.get("moving_averages") or {}
    price = tech.get("current_price")
    sma20, sma50, sma200 = ma.get("sma20"), ma.get("sma50"), ma.get("sma200")
    adx = tech.get("adx")
    if not price or not sma20:
        return 0.0, adx, "neutral"

    score = 0.0
    if price > sma20:
        score += 0.35
    else:
        score -= 0.35
    if sma50:
        score += 0.25 if sma20 > sma50 else -0.25
    if sma200:
        score += 0.20 if price > sma200 else -0.20

    # ADX gates the magnitude — weak trend < 20 → halve.
    if adx is not None:
        if adx < 20:
            score *= 0.5
        elif adx > 30:
            score *= 1.1
    score = clip(score)
    return score, adx, _direction(score)


def momentum_score(tech: dict[str, Any]) -> tuple[float, Optional[float], str]:
    rsi = tech.get("rsi")
    macd = tech.get("macd") or {}
    score = 0.0
    if rsi is not None:
        # RSI 50 = neutral; saturates at ±1 around RSI 30/70.
        score += clip((rsi - 50) / 20.0)
    if macd.get("macd_line") is not None and macd.get("signal_line") is not None:
        score += 0.4 if macd["macd_line"] > macd["signal_line"] else -0.4
    score = clip(score / 1.4)  # normalise — max raw is 1.0 + 0.4
    return score, rsi, _direction(score)


def volume_delivery_score(
    tech: dict[str, Any], delivery_pct: Optional[float]
) -> tuple[float, Optional[float], str]:
    cur = tech.get("volume_current")
    avg = tech.get("volume_avg_20")
    if not cur or not avg or avg == 0:
        return 0.0, delivery_pct, "neutral"
    ratio = cur / avg
    score = clip((ratio - 1.0) / 2.0)
    if delivery_pct is not None:
        if delivery_pct >= 60:
            score += 0.3
        elif delivery_pct < 30:
            score -= 0.3
    score = clip(score)
    return score, delivery_pct, _direction(score)


def fno_score(
    options: Optional[dict[str, Any]], price_change_pct: float
) -> tuple[float, Optional[float], str, Optional[FnoSignal]]:
    if not options:
        return 0.0, None, "neutral", None
    pcr = options.get("pcr_oi")
    if pcr is None:
        return 0.0, None, "neutral", None
    score = clip((pcr - 1.0) / 0.5)

    total_oi_chg = (options.get("total_pe_oi", 0) or 0) - (options.get("total_ce_oi", 0) or 0)
    fno: Optional[FnoSignal]
    if price_change_pct > 0 and total_oi_chg > 0:
        fno = "LONG_BUILDUP"
    elif price_change_pct < 0 and total_oi_chg > 0:
        fno = "SHORT_BUILDUP"
    elif price_change_pct < 0 and total_oi_chg < 0:
        fno = "LONG_UNWINDING"
    elif price_change_pct > 0 and total_oi_chg < 0:
        fno = "SHORT_COVERING"
    else:
        fno = None
    return score, pcr, _direction(score), fno


def fii_dii_score(
    fii_dii: dict[str, Any],
) -> tuple[float, Optional[float], str, Optional[FiiDiiSignal]]:
    fii_net = fii_dii.get("fii_net")
    if fii_net is None:
        return 0.0, None, "neutral", None
    score = clip(fii_net / 1500.0)
    sig: FiiDiiSignal = "INFLOW" if score > 0.2 else ("OUTFLOW" if score < -0.2 else "NEUTRAL")
    return score, fii_net, _direction(score), sig


def rs_score(rs_rank: Optional[int]) -> tuple[float, Optional[float], str]:
    if rs_rank is None:
        return 0.0, None, "neutral"
    score = clip((rs_rank - 50) / 50.0)
    return score, float(rs_rank), _direction(score)


def news_sentiment_score(
    articles: list[dict[str, Any]],
    announcements: Optional[list[dict[str, Any]]] = None,
) -> tuple[float, Optional[float], str]:
    """Confidence + recency weighted blend of news + NSE announcements.

    Delegates to `sentiment_nlp.combined_news_score` so the rule is:
      score_per_article = finbert_score × recency_weight × confidence_weight
    and announcements get a domain-prior blended with FinBERT on subject
    text. See `sentiment_nlp.py` for the weighting math.

    Returns 0.0 cleanly when coverage is thin so unloved smallcaps don't
    get penalised, but `coverage='high'` is treated as a real signal.
    """
    from app.services.sentiment_nlp import combined_news_score
    if not articles and not announcements:
        return 0.0, None, "neutral"
    try:
        # FinBERT scorer is heavy; pass None unless someone wants to wire
        # it from the caller. Article scores already came from FinBERT in
        # `sentiment.fetch_rss_feed`.
        result = combined_news_score(articles, announcements, finbert_score_fn=None)
    except Exception:
        return 0.0, None, "neutral"
    raw = result.get("score", 0.0)
    # Saturate at ±0.5 — sentiment is noisy and shouldn't dominate.
    s = clip(raw / 0.5)
    return s, round(raw, 3), _direction(s)


def fundamentals_score(fund: Optional[dict[str, Any]]) -> tuple[float, Optional[float], str]:
    """Quality gate from PE / ROE / D/E plus deep-fundamentals composite.

    Priority order:
      1. `deep_fundamentals.composite_score` (cash flow + balance sheet +
         earnings quality + moat) — most informative when available.
      2. `fundamental_valuation` enhanced score (existing path).
      3. Legacy PE/ROE/D/E rubric (last resort, kept for back-compat).
    """
    if not fund:
        return 0.0, None, "neutral"
    deep = fund.get("deep_fundamentals") if isinstance(fund, dict) else None
    if isinstance(deep, dict) and deep.get("composite_score", 0) > 0:
        composite = float(deep["composite_score"])  # 0..100
        # Map 0..100 → -1..+1 with neutral midpoint 50. Elite (80+)
        # contributes +0.6 → +1.0; low-quality (<25) contributes negatively.
        score = clip((composite - 50.0) / 50.0)
        return score, composite, _direction(score)
    enhanced = fund.get("fundamental_valuation") if isinstance(fund, dict) else None
    if isinstance(enhanced, dict) and enhanced.get("available"):
        score = clip(float(enhanced.get("normalized_score") or 0.0))
        return score, float(enhanced.get("score") or 50), _direction(score)
    val = fund.get("valuation") or {}
    prof = fund.get("profitability") or {}
    fh = fund.get("financial_health") or {}

    pe = val.get("pe")
    roe = prof.get("roe")
    de = fh.get("debt_to_equity")

    score = 0.0
    if pe is not None:
        # 10–25 ideal, 25–40 OK, < 5 or > 60 penalised.
        if 10 <= pe <= 25:
            score += 0.4
        elif 25 < pe <= 40:
            score += 0.1
        elif pe > 60 or pe <= 0:
            score -= 0.4
    if roe is not None:
        if roe > 0.20:
            score += 0.4
        elif roe > 0.12:
            score += 0.2
        elif roe < 0:
            score -= 0.4
    if de is not None:
        de_ratio = de / 100.0 if de > 10 else de  # screener ships %, yfinance ships ratio
        if de_ratio < 0.5:
            score += 0.2
        elif de_ratio > 2.0:
            score -= 0.3

    score = clip(score)
    # Headline value: composite "fundamental snapshot" — pick PE as the
    # representative number for the radar tooltip.
    return score, pe, _direction(score)


def weekly_trend_score(weekly_tech: Optional[dict[str, Any]]) -> tuple[float, Optional[float], str]:
    """Higher-timeframe (weekly) trend confirmation for swing/positional.

    Weekly bars are computed by resampling the daily history. Returns 0
    when not enough data to form a weekly view (e.g. < 12 weeks).
    """
    if not weekly_tech:
        return 0.0, None, "neutral"
    s, v, d = trend_score(weekly_tech)
    return s, v, d


def volatility_score(tech: dict[str, Any]) -> tuple[float, Optional[float], str]:
    # ATR % > 5 → very volatile, penalise (we want clean trends, not chop).
    atr_pct = tech.get("atr_pct")
    if atr_pct is None:
        return 0.0, None, "neutral"
    if atr_pct > 5:
        return -0.6, atr_pct, "bearish"
    if atr_pct < 1:
        return 0.2, atr_pct, "bullish"
    return 0.0, atr_pct, "neutral"


def entry_sl_targets(
    price: float, atr: Optional[float], horizon: Horizon, direction_up: bool
) -> tuple[float, float, float, Optional[float]]:
    """Return (entry, sl, t1, t2). Falls back to fixed % bands if ATR is None."""
    sl_m, t1_m, t2_m = HORIZON_ATR_BANDS[horizon]
    if atr is None or atr <= 0:
        # Why fallback: micro-caps frequently lack 14d ATR. 2% is a sane default.
        atr = price * 0.02
    if direction_up:
        return price, max(0.01, price - sl_m * atr), price + t1_m * atr, price + t2_m * atr
    return price, price + sl_m * atr, max(0.01, price - t1_m * atr), max(0.01, price - t2_m * atr)

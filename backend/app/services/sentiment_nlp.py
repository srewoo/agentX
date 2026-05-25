from __future__ import annotations
"""NLP sentiment aggregation with recency + confidence weighting.

The existing `sentiment.py` already runs FinBERT (`ProsusAI/finbert`) on
RSS articles, but downstream callers were just averaging the raw scores
— so a 13-day-old article weighted the same as a 1-hour-old one, and a
low-confidence FinBERT call weighted the same as a high-confidence one.

This module fixes that and also scores NSE corporate-action /
announcement subjects (which the engine was previously using only for
the earnings blackout, never for sentiment).

  • `aggregate_articles_sentiment` — confidence × recency weighted mean
  • `score_announcements`         — FinBERT/keyword score per NSE action
  • `combined_news_score`         — single -1..+1 score the factor uses

Pure functions, no I/O — caller passes already-fetched data.
"""
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Half-life: a 24h-old article carries ~50% weight of a fresh one.
_RECENCY_HALF_LIFE_HOURS = 24.0

# Minimum confidence to trust a FinBERT score; below this we down-weight.
_MIN_CONFIDENCE = 0.55


def _hours_old(published: Optional[str], now: datetime) -> float:
    if not published:
        return _RECENCY_HALF_LIFE_HOURS * 4  # treat undated as quite old
    try:
        pub = datetime.fromisoformat(published.replace("Z", "+00:00"))
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        return max(0.0, (now - pub).total_seconds() / 3600.0)
    except Exception:
        return _RECENCY_HALF_LIFE_HOURS * 4


def _recency_weight(hours: float) -> float:
    return 0.5 ** (hours / _RECENCY_HALF_LIFE_HOURS)


def aggregate_articles_sentiment(
    articles: list[dict[str, Any]], now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Weighted aggregate of per-article FinBERT scores.

    Each article contributes `score × recency_weight × confidence_weight`.
    Confidence weight ramps from 0.5 (low conf) to 1.0 (high conf) — we
    *never* zero out a low-confidence call, only down-weight it.
    Returns the weighted score, effective sample count, and a coverage
    flag so the engine can demote unloved smallcaps without penalising
    high-signal coverage.
    """
    if not articles:
        return {"score": 0.0, "n": 0, "effective_n": 0.0, "coverage": "none"}
    now = now or datetime.now(timezone.utc)

    weighted_sum = 0.0
    weight_sum = 0.0
    effective_n = 0.0
    for art in articles:
        s = art.get("sentiment_score")
        if s is None:
            continue
        try:
            s = float(s)
        except Exception:
            continue
        # FinBERT score returned in `sentiment.py` already encodes confidence
        # in magnitude (label_conf signed by direction). We still apply a
        # softer down-weight for borderline cases.
        conf = abs(s)
        conf_w = 0.5 if conf < _MIN_CONFIDENCE else 1.0
        rec_w = _recency_weight(_hours_old(art.get("published"), now))
        w = conf_w * rec_w
        weighted_sum += s * w
        weight_sum += w
        effective_n += w

    if weight_sum < 1e-6:
        return {"score": 0.0, "n": len(articles), "effective_n": 0.0, "coverage": "stale"}

    score = max(-1.0, min(1.0, weighted_sum / weight_sum))
    coverage = "high" if effective_n >= 3 else ("medium" if effective_n >= 1 else "low")
    return {
        "score": round(score, 3),
        "n": len(articles),
        "effective_n": round(effective_n, 2),
        "coverage": coverage,
    }


# Domain priors for NSE corporate-action subjects. These run as a fast
# keyword pass first; if FinBERT is available the subject text is also
# scored through it and the two are blended.
_ACTION_PRIORS: dict[str, float] = {
    "buyback": 0.6, "bonus": 0.4, "dividend": 0.25, "stock split": 0.2,
    "rights": -0.15, "preferential": 0.1, "amalgamation": 0.0,
    "scheme of arrangement": 0.05, "demerger": 0.15,
    "results": 0.0,  # results subjects need the body text; neutral prior
    "credit rating": 0.0, "open offer": 0.1, "qip": -0.1,
    "fundraising": -0.1, "loss": -0.4, "downgrade": -0.4,
    "fraud": -0.8, "investigation": -0.6, "default": -0.7,
    "resignation of cfo": -0.3, "resignation of md": -0.3,
}


def _prior_for_subject(subject: str) -> Optional[float]:
    s = (subject or "").lower()
    matches = [v for k, v in _ACTION_PRIORS.items() if k in s]
    if not matches:
        return None
    # Average matched priors so "buyback + bonus" doesn't sum > 1.
    return sum(matches) / len(matches)


def score_announcements(
    announcements: list[dict[str, Any]],
    *,
    finbert_score_fn: Optional[Any] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Score a list of NSE announcement/corporate-action dicts.

    `finbert_score_fn(text) -> (score, label)` is optional; when supplied
    we blend the model score with the domain prior 50/50. When omitted
    we use the prior alone (still better than the prior code path which
    ignored these entirely).
    """
    if not announcements:
        return {"score": 0.0, "n": 0}
    now = now or datetime.now(timezone.utc)
    weighted_sum = 0.0
    weight_sum = 0.0
    for a in announcements:
        subject = (a.get("subject") or a.get("action_type") or "").strip()
        if not subject:
            continue
        prior = _prior_for_subject(subject)
        model = None
        if finbert_score_fn is not None:
            try:
                model_score, _ = finbert_score_fn(subject)
                model = float(model_score)
            except Exception:
                model = None
        if prior is None and model is None:
            continue
        if prior is not None and model is not None:
            s = 0.5 * prior + 0.5 * model
        else:
            s = prior if prior is not None else model
        rec_w = _recency_weight(_hours_old(a.get("ex_date") or a.get("date"), now))
        weighted_sum += s * rec_w
        weight_sum += rec_w

    if weight_sum < 1e-6:
        return {"score": 0.0, "n": len(announcements)}
    return {
        "score": round(max(-1.0, min(1.0, weighted_sum / weight_sum)), 3),
        "n": len(announcements),
    }


def combined_news_score(
    articles: list[dict[str, Any]],
    announcements: Optional[list[dict[str, Any]]] = None,
    *,
    finbert_score_fn: Optional[Any] = None,
) -> dict[str, Any]:
    """Single -1..+1 sentiment score the recommendation engine should use.

    Combines RSS-article sentiment (FinBERT) with NSE-announcement
    sentiment, weighted by their effective sample sizes so unloved
    smallcaps don't get noise-amplified.
    """
    art = aggregate_articles_sentiment(articles or [])
    ann = score_announcements(announcements or [], finbert_score_fn=finbert_score_fn)
    w_art = art.get("effective_n", 0.0)
    w_ann = float(ann.get("n", 0))
    total = w_art + w_ann
    if total < 0.1:
        return {"score": 0.0, "articles": art, "announcements": ann, "coverage": "none"}
    blended = (art["score"] * w_art + ann["score"] * w_ann) / total
    return {
        "score": round(blended, 3),
        "articles": art,
        "announcements": ann,
        "coverage": art.get("coverage", "none"),
    }

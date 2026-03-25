from __future__ import annotations
"""
News Sentiment Analysis for Indian Stock Market.
Forked from FinSight/backend/sentiment.py.
Primary: FinBERT (ProsusAI/finbert) transformer model.
Fallback: keyword-only scoring (fast, no LLM cost).
LLM sentiment is opt-in (used for watchlist stocks during scan).
"""
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    import feedparser
    FEEDPARSER_AVAILABLE = True
except ImportError:
    FEEDPARSER_AVAILABLE = False
    logger.warning("feedparser not installed. News features disabled.")

# ---------------------------------------------------------------------------
# FinBERT lazy-loaded singleton
# ---------------------------------------------------------------------------
_finbert_pipeline = None
FINBERT_AVAILABLE = True  # flipped to False after first failure to avoid repeated attempts


def _get_finbert():
    """Return the FinBERT pipeline, or None if unavailable."""
    global _finbert_pipeline, FINBERT_AVAILABLE
    if not FINBERT_AVAILABLE:
        return None
    if _finbert_pipeline is None:
        try:
            from transformers import pipeline
            _finbert_pipeline = pipeline(
                "sentiment-analysis",
                model="ProsusAI/finbert",
                device=-1,
                truncation=True,
                max_length=512,
            )
            logger.info("FinBERT model loaded successfully")
        except Exception as e:
            logger.warning(f"FinBERT unavailable, falling back to keywords: {e}")
            FINBERT_AVAILABLE = False
            _finbert_pipeline = None
    return _finbert_pipeline


# ---------------------------------------------------------------------------
# Indian market RSS feeds
# ---------------------------------------------------------------------------
INDIA_RSS_FEEDS = [
    {"name": "Moneycontrol - Market News", "url": "https://www.moneycontrol.com/rss/marketnews.xml"},
    {"name": "Economic Times - Markets", "url": "https://economictimes.indiatimes.com/markets/rssfeed/1998036.cms"},
    {"name": "Business Standard - Markets", "url": "https://www.business-standard.com/rss/markets-113.xml"},
]

# ---------------------------------------------------------------------------
# Keyword lists (used as fallback)
# ---------------------------------------------------------------------------
POSITIVE_KEYWORDS = [
    "surge", "soar", "jump", "gain", "rally", "hit high", "record high", "outperform",
    "beat estimates", "upgrade", "bullish", "buy rating", "target raised", "profit up",
    "revenue growth", "strong quarter", "positive outlook", "accumulation", "breakout",
    "dividend", "bonus", "buyback", "order win", "expansion", "growth", "recovery", "momentum",
]

NEGATIVE_KEYWORDS = [
    "crash", "plunge", "tank", "slump", "decline", "fall", "drop", "hit low",
    "52-week low", "downgrade", "sell rating", "target cut", "loss", "miss estimates",
    "bearish", "profit down", "revenue decline", "weak quarter", "negative outlook",
    "distribution", "breakdown", "underperform", "concern", "risk",
    "regulatory", "investigation", "fraud", "default", "bankruptcy",
]

NEUTRAL_KEYWORDS = [
    "stable", "unchanged", "flat", "range-bound", "sideways", "consolidate",
    "hold", "neutral", "wait and watch", "in-line",
]


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def _clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Sentiment scoring
# ---------------------------------------------------------------------------
def _keyword_sentiment(text: str) -> tuple[float, str]:
    """Keyword-based sentiment scoring (fallback). Returns (score -1..1, label)."""
    if not text:
        return 0.0, "neutral"

    text_lower = text.lower()
    pos = sum(1 for kw in POSITIVE_KEYWORDS if kw in text_lower)
    neg = sum(1 for kw in NEGATIVE_KEYWORDS if kw in text_lower)
    neu = sum(1 for kw in NEUTRAL_KEYWORDS if kw in text_lower)

    total = pos + neg + neu
    if total == 0:
        return 0.0, "neutral"

    score = max(-1.0, min(1.0, (pos - neg) / max(total, 1)))
    label = "positive" if score > 0.2 else ("negative" if score < -0.2 else "neutral")
    return round(score, 3), label


def _finbert_to_score(result: dict) -> tuple[float, str]:
    """Convert a single FinBERT result dict to (score -1..1, label)."""
    label = result["label"].lower()  # positive / negative / neutral
    confidence = result["score"]     # 0..1

    if label == "positive":
        score = confidence
    elif label == "negative":
        score = -confidence
    else:
        # neutral — small value biased by confidence
        score = 0.0 + (confidence * 0.05)

    score = max(-1.0, min(1.0, score))
    out_label = "positive" if score > 0.2 else ("negative" if score < -0.2 else "neutral")
    return round(score, 3), out_label


def calculate_sentiment(text: str) -> tuple[float, str]:
    """Score sentiment for a single text. Uses FinBERT when available, falls back to keywords."""
    if not text:
        return 0.0, "neutral"

    # Try FinBERT first
    pipe = _get_finbert()
    if pipe is not None:
        try:
            # Truncate to ~512 tokens worth of text (roughly 2048 chars)
            truncated = text[:2048]
            result = pipe(truncated, truncation=True, max_length=512)
            if result and isinstance(result, list) and len(result) > 0:
                return _finbert_to_score(result[0])
        except Exception as e:
            logger.warning(f"FinBERT inference failed, falling back to keywords: {e}")

    # Fallback to keyword method
    return _keyword_sentiment(text)


def _batch_finbert_sentiment(texts: list[str]) -> list[tuple[float, str]]:
    """Score sentiment for a batch of texts using FinBERT. Falls back per-text on failure."""
    pipe = _get_finbert()
    if pipe is None:
        return [_keyword_sentiment(t) for t in texts]

    try:
        truncated = [t[:2048] for t in texts]
        results = pipe(truncated, truncation=True, max_length=512, batch_size=len(truncated))
        return [_finbert_to_score(r) for r in results]
    except Exception as e:
        logger.warning(f"FinBERT batch inference failed, falling back to keywords: {e}")
        return [_keyword_sentiment(t) for t in texts]


# ---------------------------------------------------------------------------
# Symbol extraction
# ---------------------------------------------------------------------------
def extract_symbols(text: str, known_symbols: list[str]) -> list[str]:
    text_upper = text.upper()
    return [sym for sym in known_symbols if sym in text_upper][:5]


# ---------------------------------------------------------------------------
# RSS feed fetching
# ---------------------------------------------------------------------------
async def fetch_rss_feed(url: str, source_name: str, timeout: int = 10) -> list[dict[str, Any]]:
    """Fetch and parse RSS feed with FinBERT sentiment scoring (batched)."""
    if not FEEDPARSER_AVAILABLE:
        return []
    try:
        feed = feedparser.parse(url, request_headers={"User-Agent": "StockPilot/1.0"})
        entries = feed.entries[:10]
        if not entries:
            return []

        # Prepare texts and metadata
        titles = []
        summaries = []
        combined_texts = []
        entry_meta = []
        for entry in entries:
            title = _clean_text(entry.get("title", ""))
            summary = _clean_text(entry.get("summary", entry.get("description", "")))
            combined = f"{title} {summary}"
            titles.append(title)
            summaries.append(summary)
            combined_texts.append(combined)
            entry_meta.append({
                "published": entry.get("published", datetime.now(timezone.utc).isoformat()),
                "link": entry.get("link", ""),
            })

        # Batch FinBERT scoring (up to 10 texts at once)
        sentiment_results = _batch_finbert_sentiment(combined_texts)

        articles = []
        for i, (score, label) in enumerate(sentiment_results):
            articles.append({
                "title": titles[i],
                "source": source_name,
                "published": entry_meta[i]["published"],
                "link": entry_meta[i]["link"],
                "summary": summaries[i][:300],
                "sentiment_score": score,
                "sentiment_label": label,
            })
        return articles
    except Exception as e:
        logger.warning(f"Failed to fetch RSS feed {source_name}: {e}")
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def get_market_news(limit: int = 20) -> list[dict[str, Any]]:
    """Fetch general market news from multiple RSS sources."""
    if not FEEDPARSER_AVAILABLE:
        return []

    all_articles: list[dict] = []
    for feed_info in INDIA_RSS_FEEDS:
        articles = await fetch_rss_feed(feed_info["url"], feed_info["name"])
        all_articles.extend(articles)

    all_articles.sort(key=lambda x: x.get("published", ""), reverse=True)

    known_symbols = [
        "NIFTY", "SENSEX", "RELIANCE", "TCS", "HDFCBANK", "INFY",
        "ICICIBANK", "SBIN", "TATAMOTORS", "BAJFINANCE",
    ]
    for article in all_articles:
        text = f"{article['title']} {article['summary']}"
        article["relevance_symbols"] = extract_symbols(text, known_symbols)

    return all_articles[:limit]


async def get_stock_news(symbol: str, limit: int = 10) -> list[dict[str, Any]]:
    """Fetch news relevant to a specific stock symbol."""
    if not FEEDPARSER_AVAILABLE:
        return []

    base_sym = symbol.replace(".NS", "").replace(".BO", "").upper()
    market_news = await get_market_news(limit=50)

    relevant = [a for a in market_news if base_sym in a.get("relevance_symbols", [])]
    seen = set()
    unique = []
    for a in relevant:
        link = a.get("link", "")
        if link not in seen:
            seen.add(link)
            unique.append(a)

    unique.sort(key=lambda x: x.get("published", ""), reverse=True)
    return unique[:limit]


async def get_sentiment_summary(symbols: Optional[list[str]] = None) -> dict[str, Any]:
    """Get overall market sentiment summary."""
    news = await get_market_news(limit=50)
    if not news:
        return {
            "overall_sentiment": "neutral",
            "overall_score": 0.0,
            "articles_count": 0,
            "positive_count": 0,
            "negative_count": 0,
            "neutral_count": 0,
        }

    scores = [a["sentiment_score"] for a in news]
    avg_score = sum(scores) / len(scores)
    pos = sum(1 for s in scores if s > 0.2)
    neg = sum(1 for s in scores if s < -0.2)

    return {
        "overall_sentiment": "positive" if avg_score > 0.2 else ("negative" if avg_score < -0.2 else "neutral"),
        "overall_score": round(avg_score, 3),
        "articles_count": len(news),
        "positive_count": pos,
        "negative_count": neg,
        "neutral_count": len(news) - pos - neg,
        "latest_news": news[:10],
    }

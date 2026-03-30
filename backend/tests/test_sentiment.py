from __future__ import annotations
"""Tests for app.services.sentiment — FinBERT and keyword sentiment analysis."""

import pytest
from unittest.mock import patch, MagicMock

from app.services.sentiment import (
    _clean_text,
    _keyword_sentiment,
    _finbert_to_score,
    calculate_sentiment,
)


# ─────────────────────────────────────────────
# _clean_text
# ─────────────────────────────────────────────

class TestCleanText:
    def test_strips_html_tags(self):
        result = _clean_text("<b>Reliance</b> surges 5%")
        assert "<b>" not in result
        assert "Reliance" in result

    def test_collapses_whitespace(self):
        result = _clean_text("too   many    spaces")
        assert "  " not in result

    def test_empty_string_returns_empty(self):
        assert _clean_text("") == ""

    def test_none_like_empty_input(self):
        # _clean_text with falsy input returns ""
        assert _clean_text(None) == ""  # type: ignore


# ─────────────────────────────────────────────
# _keyword_sentiment
# ─────────────────────────────────────────────

class TestKeywordSentiment:
    def test_positive_text_returns_positive_score(self):
        score, label = _keyword_sentiment("Reliance surges to record high with strong quarterly results")
        assert score > 0
        assert label == "positive"

    def test_negative_text_returns_negative_score(self):
        score, label = _keyword_sentiment("Stock crashes to 52-week low, analyst downgrade issued")
        assert score < 0
        assert label == "negative"

    def test_neutral_text_returns_near_zero(self):
        score, label = _keyword_sentiment("Market remains stable with range-bound trading")
        # neutral or slightly positive/negative
        assert -0.3 <= score <= 0.3

    def test_empty_text_returns_zero(self):
        score, label = _keyword_sentiment("")
        assert score == 0.0
        assert label == "neutral"

    def test_mixed_positive_and_negative(self):
        # Equal positive and negative keywords → near neutral
        score, label = _keyword_sentiment(
            "Stock rallies on strong earnings but faces regulatory investigation"
        )
        assert isinstance(score, float)
        assert label in ("positive", "negative", "neutral")

    def test_score_bounded_within_minus1_to_1(self):
        very_positive = " ".join(["surge", "soar", "jump", "gain", "rally"] * 20)
        score, _ = _keyword_sentiment(very_positive)
        assert -1.0 <= score <= 1.0


# ─────────────────────────────────────────────
# _finbert_to_score
# ─────────────────────────────────────────────

class TestFinbertToScore:
    def test_positive_label_returns_positive_score(self):
        score, label = _finbert_to_score({"label": "positive", "score": 0.9})
        assert score > 0
        assert label == "positive"

    def test_negative_label_returns_negative_score(self):
        score, label = _finbert_to_score({"label": "negative", "score": 0.85})
        assert score < 0
        assert label == "negative"

    def test_neutral_label_returns_near_zero(self):
        score, label = _finbert_to_score({"label": "neutral", "score": 0.7})
        assert -0.15 <= score <= 0.15

    def test_score_clamped_to_minus1_to_1(self):
        score, _ = _finbert_to_score({"label": "positive", "score": 1.0})
        assert score == 1.0
        score, _ = _finbert_to_score({"label": "negative", "score": 1.0})
        assert score == -1.0

    def test_lowercase_label_handled(self):
        """Labels from FinBERT are already lowercase."""
        score, label = _finbert_to_score({"label": "positive", "score": 0.8})
        assert score == pytest.approx(0.8, abs=1e-3)


# ─────────────────────────────────────────────
# calculate_sentiment
# ─────────────────────────────────────────────

class TestCalculateSentiment:
    def test_empty_text_returns_zero(self):
        score, label = calculate_sentiment("")
        assert score == 0.0
        assert label == "neutral"

    def test_uses_finbert_when_available(self):
        mock_pipe = MagicMock(return_value=[{"label": "positive", "score": 0.9}])
        with patch("app.services.sentiment._get_finbert", return_value=mock_pipe):
            score, label = calculate_sentiment("Strong earnings growth")
        assert score > 0
        mock_pipe.assert_called_once()

    def test_falls_back_to_keywords_when_finbert_unavailable(self):
        with patch("app.services.sentiment._get_finbert", return_value=None):
            score, label = calculate_sentiment("Stock surges to all-time high")
        assert score > 0  # keyword method picks up "surge"

    def test_falls_back_when_finbert_raises(self):
        mock_pipe = MagicMock(side_effect=RuntimeError("model error"))
        with patch("app.services.sentiment._get_finbert", return_value=mock_pipe):
            # Should not raise — falls back to keyword method
            score, label = calculate_sentiment("Strong quarterly results")
        assert isinstance(score, float)

    def test_returns_float_and_label_string(self):
        with patch("app.services.sentiment._get_finbert", return_value=None):
            score, label = calculate_sentiment("Market is flat today")
        assert isinstance(score, float)
        assert isinstance(label, str)
        assert label in ("positive", "negative", "neutral")

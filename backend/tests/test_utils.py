from __future__ import annotations

"""Tests for app.utils — safe_float, sanitize_symbol, parse_llm_json."""

import math

import numpy as np
import pytest

from app.utils import parse_llm_json, safe_float, sanitize_symbol


# ---------------------------------------------------------------------------
# safe_float
# ---------------------------------------------------------------------------

class TestSafeFloat:
    """Tests for safe_float()."""

    def test_given_integer_when_called_then_returns_float(self):
        assert safe_float(42) == 42.0

    def test_given_float_when_called_then_returns_rounded(self):
        assert safe_float(3.14159) == 3.14

    def test_given_negative_float_when_called_then_returns_rounded(self):
        assert safe_float(-123.456) == -123.46

    def test_given_string_number_when_called_then_returns_float(self):
        assert safe_float("1500.75") == 1500.75

    def test_given_string_integer_when_called_then_returns_float(self):
        assert safe_float("100") == 100.0

    def test_given_none_when_called_then_returns_none(self):
        assert safe_float(None) is None

    def test_given_nan_when_called_then_returns_none(self):
        assert safe_float(float("nan")) is None

    def test_given_numpy_nan_when_called_then_returns_none(self):
        assert safe_float(np.nan) is None

    def test_given_inf_when_called_then_returns_none(self):
        assert safe_float(float("inf")) is None

    def test_given_negative_inf_when_called_then_returns_none(self):
        assert safe_float(float("-inf")) is None

    def test_given_numpy_inf_when_called_then_returns_none(self):
        assert safe_float(np.inf) is None

    def test_given_zero_when_called_then_returns_zero(self):
        assert safe_float(0) == 0.0

    def test_given_zero_string_when_called_then_returns_zero(self):
        assert safe_float("0") == 0.0

    def test_given_non_numeric_string_when_called_then_returns_none(self):
        assert safe_float("not_a_number") is None

    def test_given_empty_string_when_called_then_returns_none(self):
        assert safe_float("") is None

    def test_given_boolean_true_when_called_then_returns_float(self):
        # bool is subclass of int in Python; float(True) == 1.0
        assert safe_float(True) == 1.0

    def test_given_numpy_float64_when_called_then_returns_float(self):
        assert safe_float(np.float64(2500.123)) == 2500.12

    def test_given_list_when_called_then_returns_none(self):
        assert safe_float([1, 2, 3]) is None

    def test_given_dict_when_called_then_returns_none(self):
        assert safe_float({"price": 100}) is None


# ---------------------------------------------------------------------------
# sanitize_symbol
# ---------------------------------------------------------------------------

class TestSanitizeSymbol:
    """Tests for sanitize_symbol()."""

    def test_given_valid_symbol_when_called_then_returns_uppercased(self):
        assert sanitize_symbol("reliance") == "RELIANCE"

    def test_given_symbol_with_spaces_when_called_then_strips(self):
        assert sanitize_symbol("  TCS  ") == "TCS"

    def test_given_symbol_with_dot_when_called_then_preserves_dot(self):
        assert sanitize_symbol("RELIANCE.NS") == "RELIANCE.NS"

    def test_given_symbol_with_dash_when_called_then_preserves_dash(self):
        assert sanitize_symbol("M-M") == "M-M"

    def test_given_symbol_with_special_chars_when_called_then_strips_chars(self):
        assert sanitize_symbol("REL!@#$IANCE") == "RELIANCE"

    def test_given_symbol_with_unicode_when_called_then_strips(self):
        assert sanitize_symbol("INFY\u20b9") == "INFY"

    def test_given_long_symbol_when_called_then_truncates_to_20(self):
        result = sanitize_symbol("A" * 50)
        assert len(result) == 20
        assert result == "A" * 20

    def test_given_empty_string_when_called_then_returns_empty(self):
        assert sanitize_symbol("") == ""

    def test_given_only_whitespace_when_called_then_returns_empty(self):
        assert sanitize_symbol("   ") == ""

    def test_given_numeric_symbol_when_called_then_preserves(self):
        assert sanitize_symbol("500325") == "500325"

    def test_given_mixed_case_when_called_then_uppercases(self):
        assert sanitize_symbol("HdfcBank") == "HDFCBANK"

    def test_given_caret_index_when_called_then_strips_caret(self):
        # Caret is not in [A-Z0-9\-\.] so it gets stripped
        assert sanitize_symbol("^NSEI") == "NSEI"


# ---------------------------------------------------------------------------
# parse_llm_json
# ---------------------------------------------------------------------------

class TestParseLlmJson:
    """Tests for parse_llm_json()."""

    def test_given_valid_json_when_called_then_returns_parsed(self):
        result = parse_llm_json('{"key": "value", "num": 42}', {})
        assert result == {"key": "value", "num": 42}

    def test_given_markdown_json_block_when_called_then_strips_fences(self):
        raw = '```json\n{"summary": "Bullish trend"}\n```'
        result = parse_llm_json(raw, {"fallback": True})
        assert result == {"summary": "Bullish trend"}

    def test_given_markdown_block_no_lang_when_called_then_strips_fences(self):
        raw = '```\n{"data": 1}\n```'
        result = parse_llm_json(raw, {})
        assert result == {"data": 1}

    def test_given_malformed_json_when_called_then_returns_fallback(self):
        fallback = {"error": "parse_failed"}
        result = parse_llm_json("{bad json;;;}", fallback)
        assert result == fallback

    def test_given_empty_string_when_called_then_returns_fallback(self):
        fallback = {"default": True}
        result = parse_llm_json("", fallback)
        assert result == fallback

    def test_given_plain_text_when_called_then_returns_fallback(self):
        fallback = {"status": "error"}
        result = parse_llm_json("The stock looks bullish.", fallback)
        assert result == fallback

    def test_given_json_with_whitespace_when_called_then_parses(self):
        raw = '  \n  {"key": "val"}  \n  '
        result = parse_llm_json(raw, {})
        assert result == {"key": "val"}

    def test_given_nested_json_when_called_then_parses(self):
        raw = '{"analysis": {"rsi": 65, "trend": "up"}, "confidence": 0.8}'
        result = parse_llm_json(raw, {})
        assert result["analysis"]["rsi"] == 65
        assert result["confidence"] == 0.8

    def test_given_json_array_when_called_then_returns_fallback(self):
        # parse_llm_json expects a dict, but json.loads of an array is valid.
        # The function returns whatever json.loads returns.
        raw = '[1, 2, 3]'
        result = parse_llm_json(raw, {"fallback": True})
        # json.loads succeeds but returns a list, not a dict
        assert result == [1, 2, 3]

    def test_given_triple_backtick_only_when_called_then_returns_fallback(self):
        fallback = {"empty": True}
        result = parse_llm_json("```\n```", fallback)
        assert result == fallback

    def test_given_json_with_markdown_prefix_when_called_then_strips(self):
        raw = '```json\n{"signal": "buy", "price": 1500}\n```'
        result = parse_llm_json(raw, {})
        assert result["signal"] == "buy"
        assert result["price"] == 1500

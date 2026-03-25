from __future__ import annotations
"""Shared utility functions."""
import json
import logging
import re
import numpy as np

logger = logging.getLogger(__name__)


def safe_float(val) -> float | None:
    """Convert val to float, returning None for NaN/Inf/None."""
    if val is None:
        return None
    try:
        f = float(val)
        if np.isnan(f) or np.isinf(f):
            return None
        return round(f, 2)
    except (TypeError, ValueError):
        return None


def sanitize_symbol(symbol: str) -> str:
    """Sanitize stock symbol - uppercase, strip whitespace, allow only alphanum dash dot."""
    symbol = symbol.strip().upper()
    symbol = re.sub(r"[^A-Z0-9\-\.]", "", symbol)
    return symbol[:20]  # max length guard


def parse_llm_json(response: str, fallback: dict) -> dict:
    """Strip markdown fences and parse JSON; return fallback on failure."""
    try:
        clean = response.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
        if clean.endswith("```"):
            clean = clean[:-3]
        if clean.startswith("json"):
            clean = clean[4:]
        return json.loads(clean.strip())
    except (json.JSONDecodeError, Exception):
        logger.warning(f"Failed to parse LLM JSON, using fallback. Response prefix: {response[:100]}")
        return fallback

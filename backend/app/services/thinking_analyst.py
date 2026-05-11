from __future__ import annotations

"""Deep reasoning analysis for existing signal cards.

Fast scans stay deterministic. This module is invoked on demand and asks a
reasoning model to synthesize the already-collected signal, historical edge,
portfolio and market context into a structured decision review.
"""

import json
import logging
from typing import Any

import aiosqlite

from app.database import DB_PATH, _decrypt_settings_map
from app.services.llm_analyst import _build_fallback_chain, _get_api_key, _sanitize_for_prompt
from app.services.llm_client import call_llm, call_openai_responses_json
from app.services.market_data import get_market_context
from app.services.portfolio import portfolio_recommendation_context
from app.services.signal_edge import get_edge
from app.utils import parse_llm_json

logger = logging.getLogger(__name__)

_THINKING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["ACT", "WATCH", "AVOID", "EXIT_REVIEW"]},
        "confidence": {"type": "integer"},
        "summary": {"type": "string"},
        "bull_case": {"type": "array", "items": {"type": "string"}},
        "bear_case": {"type": "array", "items": {"type": "string"}},
        "invalidations": {"type": "array", "items": {"type": "string"}},
        "portfolio_note": {"type": "string"},
        "risk_controls": {"type": "array", "items": {"type": "string"}},
        "data_gaps": {"type": "array", "items": {"type": "string"}},
        "not_advice": {"type": "string"},
    },
    "required": [
        "verdict",
        "confidence",
        "summary",
        "bull_case",
        "bear_case",
        "invalidations",
        "portfolio_note",
        "risk_controls",
        "data_gaps",
        "not_advice",
    ],
    "additionalProperties": False,
}

_FALLBACK = {
    "verdict": "WATCH",
    "confidence": 50,
    "summary": "Deep analysis is unavailable. Use the deterministic signal, backtest edge, and risk controls only.",
    "bull_case": [],
    "bear_case": [],
    "invalidations": ["LLM analysis unavailable."],
    "portfolio_note": "Portfolio context unavailable.",
    "risk_controls": ["Use predefined stop-loss and position sizing."],
    "data_gaps": ["No model-generated review."],
    "not_advice": "Research signal only, not investment advice.",
}


async def _load_settings() -> dict[str, str]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT key, value FROM settings") as cur:
            rows = await cur.fetchall()
    return _decrypt_settings_map({row["key"]: row["value"] for row in rows})


async def _load_signal(signal_id: str) -> dict[str, Any] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    signal = dict(row)
    try:
        signal["metadata"] = json.loads(signal.get("metadata") or "{}")
    except Exception:
        signal["metadata"] = {}
    return signal


def _clean_list(values: Any, limit: int = 6) -> list[str]:
    if not isinstance(values, list):
        return []
    return [_sanitize_for_prompt(v, 240) for v in values[:limit] if str(v).strip()]


def _validate_deep_output(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return dict(_FALLBACK)
    verdict = str(raw.get("verdict", "WATCH")).upper()
    if verdict not in {"ACT", "WATCH", "AVOID", "EXIT_REVIEW"}:
        verdict = "WATCH"
    try:
        confidence = max(0, min(100, int(raw.get("confidence", 50))))
    except Exception:
        confidence = 50
    return {
        "verdict": verdict,
        "confidence": confidence,
        "summary": _sanitize_for_prompt(raw.get("summary"), 700),
        "bull_case": _clean_list(raw.get("bull_case")),
        "bear_case": _clean_list(raw.get("bear_case")),
        "invalidations": _clean_list(raw.get("invalidations")),
        "portfolio_note": _sanitize_for_prompt(raw.get("portfolio_note"), 300),
        "risk_controls": _clean_list(raw.get("risk_controls")),
        "data_gaps": _clean_list(raw.get("data_gaps")),
        "not_advice": _sanitize_for_prompt(
            raw.get("not_advice") or "Research signal only, not investment advice.",
            180,
        ),
    }


async def analyze_signal_deep(signal_id: str, reasoning_effort: str = "medium") -> dict[str, Any]:
    signal = await _load_signal(signal_id)
    if not signal:
        raise ValueError("Signal not found")

    settings = await _load_settings()
    symbol = signal["symbol"]
    direction = signal["direction"]
    edge = get_edge(signal["signal_type"], direction) or {}
    action = "BUY" if direction == "bullish" else ("SELL" if direction == "bearish" else "HOLD")
    sector = "Unknown"
    try:
        from app.services.data_fetcher import MAJOR_STOCKS
        sector = next((s.get("sector", "Unknown") for s in MAJOR_STOCKS if s["symbol"] == symbol), "Unknown")
    except Exception:
        pass
    try:
        portfolio = await portfolio_recommendation_context(symbol=symbol, sector=sector, action=action)
    except Exception:
        portfolio = {"available": False, "notes": ["Portfolio context unavailable."]}
    try:
        market_context = await get_market_context(symbol)
    except Exception:
        market_context = {}

    context = {
        "signal": {
            "symbol": symbol,
            "signal_type": signal["signal_type"],
            "direction": direction,
            "action": action,
            "strength": signal["strength"],
            "reason": signal["reason"],
            "risk": signal.get("risk"),
            "price": signal.get("current_price"),
            "metadata": signal.get("metadata") or {},
        },
        "historical_edge": edge,
        "portfolio": portfolio,
        "market_context": market_context,
    }
    prompt = (
        "Analyze this Indian equity signal using the supplied JSON context. "
        "Think through historical edge, confirmation quality, market context, "
        "portfolio concentration, and risk controls. Do not invent live facts. "
        "Return strict JSON matching the schema.\n\n"
        f"CONTEXT_JSON:\n{json.dumps(context, default=str)[:12000]}"
    )

    provider = settings.get("llm_provider", "gemini")
    model = settings.get("llm_model", "gemini-3.1-flash")
    openai_key = settings.get("openai_api_key", "").strip()
    system_message = (
        "You are a cautious Indian stock-market signal reviewer. You are not a financial advisor. "
        "Use only supplied data, separate bull and bear cases, and make risk controls explicit."
    )

    if openai_key:
        chosen_model = model if provider == "openai" else "gpt-5"
        raw = await call_openai_responses_json(
            model=chosen_model,
            api_key=openai_key,
            prompt=prompt,
            schema=_THINKING_SCHEMA,
            system_message=system_message,
            reasoning_effort=reasoning_effort,
            max_output_tokens=2800,
            route="signal_deep_analysis",
            symbol=symbol,
        )
        parsed = parse_llm_json(raw, _FALLBACK)
        engine = "openai_responses"
    else:
        api_key = _get_api_key(settings, provider)
        if not api_key:
            return {**_FALLBACK, "engine": "none", "symbol": symbol}
        raw = await call_llm(
            provider=provider,
            model=model,
            api_key=api_key,
            prompt=prompt,
            system_message=system_message + " Return ONLY valid JSON.",
            fallback_chain=_build_fallback_chain(settings, provider),
            route="signal_deep_analysis",
            symbol=symbol,
        )
        parsed = parse_llm_json(raw, _FALLBACK)
        engine = provider

    return {
        **_validate_deep_output(parsed),
        "engine": engine,
        "symbol": symbol,
        "reasoning_effort": reasoning_effort if engine == "openai_responses" else None,
    }

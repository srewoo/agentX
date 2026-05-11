from __future__ import annotations

"""LLM judge for recommendation evidence.

The judge does not create the recommendation. It critiques the deterministic
evidence and returns a bounded adjustment that the ensemble can apply.
"""

import json
import logging
from typing import Any

import aiosqlite

from app.database import DB_PATH, _decrypt_settings_map
from app.models.recommendation import Recommendation
from app.services.llm_analyst import _build_fallback_chain, _get_api_key, _sanitize_for_prompt
from app.services.llm_client import call_llm, call_openai_responses_json
from app.utils import parse_llm_json

logger = logging.getLogger(__name__)

_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["UPGRADE", "CONFIRM", "WATCH", "DOWNGRADE", "BLOCK"]},
        "confidence_adjustment": {"type": "integer"},
        "summary": {"type": "string"},
        "supporting_evidence": {"type": "array", "items": {"type": "string"}},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
        "missing_evidence": {"type": "array", "items": {"type": "string"}},
        "calibration_notes": {"type": "array", "items": {"type": "string"}},
        "not_advice": {"type": "string"},
    },
    "required": [
        "verdict",
        "confidence_adjustment",
        "summary",
        "supporting_evidence",
        "risk_flags",
        "missing_evidence",
        "calibration_notes",
        "not_advice",
    ],
    "additionalProperties": False,
}

_FALLBACK = {
    "verdict": "WATCH",
    "confidence_adjustment": 0,
    "summary": "LLM judge unavailable; use deterministic ensemble only.",
    "supporting_evidence": [],
    "risk_flags": ["LLM judge unavailable."],
    "missing_evidence": [],
    "calibration_notes": [],
    "not_advice": "Research signal only, not investment advice.",
}


async def _load_settings() -> dict[str, str]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT key, value FROM settings") as cur:
            rows = await cur.fetchall()
    return _decrypt_settings_map({row["key"]: row["value"] for row in rows})


def _clean_list(values: Any, limit: int = 6) -> list[str]:
    if not isinstance(values, list):
        return []
    return [_sanitize_for_prompt(v, 240) for v in values[:limit] if str(v).strip()]


def validate_judge_output(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    verdict = str(raw.get("verdict") or "WATCH").upper()
    if verdict not in {"UPGRADE", "CONFIRM", "WATCH", "DOWNGRADE", "BLOCK"}:
        verdict = "WATCH"
    try:
        adj = int(raw.get("confidence_adjustment") or 0)
    except Exception:
        adj = 0
    adj = max(-15, min(15, adj))
    return {
        "verdict": verdict,
        "confidence_adjustment": adj,
        "summary": _sanitize_for_prompt(raw.get("summary") or _FALLBACK["summary"], 700),
        "supporting_evidence": _clean_list(raw.get("supporting_evidence")),
        "risk_flags": _clean_list(raw.get("risk_flags")),
        "missing_evidence": _clean_list(raw.get("missing_evidence")),
        "calibration_notes": _clean_list(raw.get("calibration_notes")),
        "not_advice": _sanitize_for_prompt(
            raw.get("not_advice") or "Research signal only, not investment advice.",
            180,
        ),
    }


async def judge_recommendation(
    rec: Recommendation,
    *,
    evidence: dict[str, Any],
    reasoning_effort: str = "medium",
) -> dict[str, Any]:
    settings = await _load_settings()
    provider = settings.get("llm_provider", "gemini")
    model = settings.get("llm_model", "gemini-3.1-flash")
    openai_key = settings.get("openai_api_key", "").strip()

    context = {
        "recommendation": rec.model_dump(mode="json"),
        "evidence": evidence,
    }
    prompt = (
        "Critique this Indian equity recommendation. Use only supplied JSON. "
        "Judge whether the deterministic recommendation deserves an upgrade, "
        "confirmation, watch-only status, downgrade, or block. Be strict about "
        "valuation, weak historical edge, regime mismatch, concentration risk, "
        "and missing data. Return strict JSON matching the schema.\n\n"
        f"CONTEXT_JSON:\n{json.dumps(context, default=str)[:14000]}"
    )
    system_message = (
        "You are a cautious stock-recommendation judge. You are not a financial advisor. "
        "Your job is to calibrate model evidence, not to invent facts or guarantee outcomes."
    )

    try:
        if openai_key:
            chosen_model = model if provider == "openai" else "gpt-5"
            raw = await call_openai_responses_json(
                model=chosen_model,
                api_key=openai_key,
                prompt=prompt,
                schema=_JUDGE_SCHEMA,
                system_message=system_message,
                reasoning_effort=reasoning_effort,
                max_output_tokens=2600,
                route="recommendation_llm_judge",
                symbol=rec.symbol,
            )
            parsed = parse_llm_json(raw, _FALLBACK)
            engine = "openai_responses"
        else:
            api_key = _get_api_key(settings, provider)
            if not api_key:
                return {**_FALLBACK, "engine": "none", "reasoning_effort": None}
            raw = await call_llm(
                provider=provider,
                model=model,
                api_key=api_key,
                prompt=prompt,
                system_message=system_message + " Return ONLY valid JSON.",
                fallback_chain=_build_fallback_chain(settings, provider),
                route="recommendation_llm_judge",
                symbol=rec.symbol,
            )
            parsed = parse_llm_json(raw, _FALLBACK)
            engine = provider
    except Exception as exc:
        logger.warning("LLM judge failed for %s: %s", rec.symbol, exc)
        return {**_FALLBACK, "engine": "error", "reasoning_effort": None}

    return {
        **validate_judge_output(parsed),
        "engine": engine,
        "reasoning_effort": reasoning_effort if engine == "openai_responses" else None,
    }

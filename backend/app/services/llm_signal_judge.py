from __future__ import annotations
"""
Layer-2 LLM signal judge.

Architecture rule: the deterministic signal_engine (Layer 1) remains the only
source of truth for *whether* a signal exists. This module is a second pass
that lets an LLM reason qualitatively over the candidate list and either
keep, downgrade, or drop each candidate — without altering the rule-based
verdict that produced it.

Cost model: ONE batched LLM call per scan, regardless of candidate count.
Candidates are sent as a compact JSON list and the LLM returns a JSON object
keyed by signal id. Fail-open: any parse / call failure leaves verdicts unset
so the deterministic candidates pass through unchanged.
"""
import json
import logging
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.services.llm_analyst import _get_api_key, _build_fallback_chain
from app.services.llm_client import call_llm

logger = logging.getLogger(__name__)

# Cap per scan — protects token budget if Layer-1 misbehaves and emits hundreds.
_MAX_CANDIDATES_PER_CALL = 40


class JudgeVerdict(BaseModel):
    """One LLM verdict for one Layer-1 candidate."""
    id: str
    verdict: str = Field(pattern="^(keep|drop|downgrade)$")
    reason: str = Field(max_length=240)

    @field_validator("reason")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


_SYSTEM_PROMPT = (
    "You are a senior Indian equities risk reviewer. You are given a list of "
    "candidate trading signals already validated by deterministic rules "
    "(technicals, patterns, volume, RSI, MACD, etc.). Your job is to apply "
    "qualitative judgment on top: spot setups that look statistically valid "
    "but are practically weak (counter-trend, illiquid, post-earnings drift, "
    "sector in a known drawdown, broken fundamentals, recent corporate "
    "action distortion, etc.). For each candidate, return one of: "
    "'keep' — endorse as-is; 'downgrade' — keep but warn the user it's "
    "lower-conviction than the rule strength suggests; 'drop' — recommend "
    "the user ignore this signal entirely. Be conservative: when unsure, "
    "prefer 'keep'. Respond with strict JSON only — no prose, no markdown."
)


def _build_prompt(candidates: list[dict]) -> str:
    """Compact one-line-per-signal payload to keep token cost low."""
    rows = []
    for c in candidates:
        rows.append({
            "id": c["id"],
            "symbol": c["symbol"],
            "signal_type": c["signal_type"],
            "direction": c["direction"],
            "strength": c["strength"],
            "reason": (c.get("reason") or "")[:200],
            "price": c.get("current_price"),
        })
    payload = json.dumps(rows, separators=(",", ":"))
    return (
        "Here are the candidate signals. For EACH, return a verdict.\n\n"
        f"CANDIDATES (JSON):\n{payload}\n\n"
        "Respond with JSON of the form: "
        '{"verdicts":[{"id":"<signal_id>","verdict":"keep|drop|downgrade",'
        '"reason":"<one short sentence, <=200 chars>"}]}'
    )


def _parse_response(raw: str, expected_ids: set[str]) -> dict[str, JudgeVerdict]:
    """Parse LLM response into a {id: JudgeVerdict} map. Strict — caller fails open."""
    # Some providers wrap JSON in markdown despite instructions. Trim defensively.
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    data = json.loads(text)
    verdicts_raw = data.get("verdicts") if isinstance(data, dict) else None
    if not isinstance(verdicts_raw, list):
        raise ValueError("response missing 'verdicts' array")

    out: dict[str, JudgeVerdict] = {}
    for entry in verdicts_raw:
        try:
            v = JudgeVerdict(**entry)
        except ValidationError as e:
            logger.debug("judge: dropping invalid verdict entry: %s (%s)", entry, e)
            continue
        if v.id in expected_ids:
            out[v.id] = v
    return out


async def judge_signals(
    candidates: list[dict],
    settings: dict[str, Any],
) -> dict[str, JudgeVerdict]:
    """
    Run one batched LLM call over the candidate list.

    Returns a {signal_id: JudgeVerdict} map. Missing entries mean the LLM
    didn't comment on that candidate (treat as implicit 'keep').

    Fail-open: any failure (no API key, parse error, provider down, cap hit)
    returns {} so the orchestrator keeps all candidates as-is.
    """
    if not candidates:
        return {}

    provider = settings.get("llm_provider", "gemini")
    model = settings.get("llm_model", "gemini-3.1-flash")
    api_key = _get_api_key(settings, provider)
    if not api_key:
        logger.debug("judge: no API key for %s — skipping LLM layer", provider)
        return {}

    # Cap to protect token budget.
    if len(candidates) > _MAX_CANDIDATES_PER_CALL:
        # Prefer high-strength first so the LLM weighs in on the most prominent ones.
        candidates = sorted(
            candidates, key=lambda c: c.get("strength", 0), reverse=True
        )[:_MAX_CANDIDATES_PER_CALL]

    expected_ids = {c["id"] for c in candidates}
    prompt = _build_prompt(candidates)
    fallback = _build_fallback_chain(settings, provider)

    try:
        raw = await call_llm(
            provider=provider,
            model=model,
            api_key=api_key,
            prompt=prompt,
            system_message=_SYSTEM_PROMPT,
            fallback_chain=fallback,
            route="signal_judge",
            symbol=None,
        )
    except Exception as e:
        logger.warning("judge: LLM call failed, failing open: %s", e)
        return {}

    try:
        verdicts = _parse_response(raw, expected_ids)
    except Exception as e:
        logger.warning("judge: parse failed, failing open: %s; raw=%.200s", e, raw)
        return {}

    drops = sum(1 for v in verdicts.values() if v.verdict == "drop")
    downs = sum(1 for v in verdicts.values() if v.verdict == "downgrade")
    keeps = sum(1 for v in verdicts.values() if v.verdict == "keep")
    logger.info(
        "judge: %d candidates → keep=%d, downgrade=%d, drop=%d (no-verdict=%d)",
        len(candidates), keeps, downs, drops, len(candidates) - len(verdicts),
    )
    return verdicts


def is_enabled(settings: dict[str, Any]) -> bool:
    """Read the user-facing toggle. Default off."""
    val = str(settings.get("llm_judging_enabled", "false")).strip().lower()
    return val in ("true", "1", "yes", "on")

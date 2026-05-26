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

from pydantic import ValidationError

from app.services.llm_analyst import _get_api_key, _build_fallback_chain
from app.services.llm_client import call_llm
from app.services.llm_schemas import JudgeResponse, JudgeVerdict

logger = logging.getLogger(__name__)

# Cap per scan — protects token budget if Layer-1 misbehaves and emits hundreds.
_MAX_CANDIDATES_PER_CALL = 40


from app.services.llm_india_context import briefing as _india_briefing

_SYSTEM_PROMPT = (
    _india_briefing(
        include_flow=True,
        include_sector=False,
        include_red_flags=True,
        include_seasonality=False,
    )
    + "\n\nROLE: You are a senior Indian-equities risk reviewer reading a list "
    "of candidate signals that have already cleared deterministic rule checks. "
    "For each, apply qualitative judgment using the context above: spot setups "
    "that look statistically valid but are practically weak (counter-trend in "
    "a strong-trend regime, ASM/GSM-flagged scrips, F&O ban, fresh promoter "
    "selling, broken fundamentals after a sector derating, mismatched session "
    "context). Return one of: 'keep' — endorse as-is; 'downgrade' — keep but "
    "warn it's lower-conviction than the rule strength suggests; 'drop' — tell "
    "the user to ignore. Be conservative; when unsure, prefer 'keep'. The "
    "reason field should name the specific red flag (≤ 200 chars). Respond "
    "with strict JSON only — no prose, no markdown."
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
    """Parse LLM response into a {id: JudgeVerdict} map. Strict — caller fails open.

    Uses the Pydantic ``JudgeResponse`` envelope so a malformed verdict
    drops cleanly (logged) instead of poisoning the batch.
    """
    text = raw.strip()
    # Some providers wrap JSON in markdown despite instructions. Trim defensively.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    data = json.loads(text)
    verdicts_raw = data.get("verdicts") if isinstance(data, dict) else None
    if not isinstance(verdicts_raw, list):
        # Envelope-level shape failure — let the outer judge log + fail open.
        raise ValueError("response missing 'verdicts' array")

    # Per-entry Pydantic validation so a single bad row doesn't poison the
    # whole batch (matches the prior fail-tolerant semantic). The
    # JudgeResponse envelope is still the canonical shape — see
    # llm_schemas.py — but we apply it row-by-row here.
    out: dict[str, JudgeVerdict] = {}
    for entry in verdicts_raw:
        try:
            v = JudgeVerdict.model_validate(entry)
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
    # Prepend today's live macro snapshot so the LLM grounds judgment in
    # current FII/DII/VIX/USDINR/Brent rather than the static playbook alone.
    try:
        from app.services.market_snapshot import get_live_briefing_block
        prompt = f"{await get_live_briefing_block()}\n\n{prompt}"
    except Exception:
        pass
    fallback = _build_fallback_chain(settings, provider)

    # Budget: per-candidate ~60 output tokens (id+verdict+short reason) plus
    # ~1.5x slack for reasoning-model hidden tokens (gpt-5 / o-series). The
    # previous 2048 default starved reasoning models, producing empty
    # `message.content` and failing the parser on every scan.
    judge_max_tokens = max(2048, _MAX_CANDIDATES_PER_CALL * 60 * 2)

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
            max_tokens=judge_max_tokens,
        )
    except Exception as e:
        logger.warning("judge: LLM call failed, failing open: %s", e)
        return {}

    if not raw or not raw.strip():
        logger.warning(
            "judge: empty response from %s/%s (likely reasoning tokens exhausted "
            "max_completion_tokens=%d); failing open. Consider raising the budget "
            "or switching to a non-reasoning model for this route.",
            provider, model, judge_max_tokens,
        )
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

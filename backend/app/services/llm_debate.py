"""Bull/Bear/Judge adversarial debate loop.

Architecture: for high-conviction signals (strength ≥ N), run three
parallel/serial LLM prompts:

1. **Bull**:  argue the case *for* the signal (long if bullish, exit if bearish)
2. **Bear**:  argue the case *against*
3. **Judge**: read both, pick the stronger side, output calibrated confidence

The judge's verdict (bull / bear / inconclusive) is stored alongside the
existing Layer-2 ``llm_verdict`` and exposed via the same Signal payload.
The two are complementary: the Layer-2 judge filters individual setups;
the debate stress-tests the *strongest* setups for confirmation bias.

Cost model: 3 LLM calls per signal. Cap at ``_DEBATE_TOP_N`` per scan
(default 3) so the worst-case cost is 9 calls per scan regardless of how
many signals fire. Off by default — enabled via
``settings['debate_enabled']``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from pydantic import ValidationError

from app.services.llm_analyst import _get_api_key, _build_fallback_chain
from app.services.llm_client import call_llm
from app.services.llm_schemas import (
    DebateArgument,
    DebateResult,
    DebateVerdict,
)

logger = logging.getLogger(__name__)

# Number of top-strength signals to debate per scan. 3 = 9 LLM calls max,
# bounded for cost. Make this user-configurable later.
_DEBATE_TOP_N = 3


from app.services.llm_india_context import briefing as _india_briefing

_DEBATE_BRIEF = _india_briefing(
    include_flow=True,
    include_sector=True,
    include_red_flags=True,
    include_seasonality=False,
)

_BULL_PROMPT = (
    _DEBATE_BRIEF
    + "\n\nROLE: You are the senior bull-side analyst on the Indian-equities "
    "trading desk. Argue the strongest possible case FOR the trade implied "
    "by this signal. Cite concrete factors that an Indian-market specialist "
    "would notice: confirming FII/DII flow, sector index alignment, USD/INR "
    "or crude tailwinds, monthly volumes (auto/cement), promoter buying, "
    "results-day delivery %, capex/policy tailwinds, technical structure on "
    "the Nifty parent index. Be honest about conviction — if your case has "
    "to lean on weak evidence, say so via low confidence. Respond with "
    "strict JSON only — no prose, no markdown."
)

_BEAR_PROMPT = (
    _DEBATE_BRIEF
    + "\n\nROLE: You are the senior bear-side analyst on the Indian-equities "
    "trading desk. Argue the strongest possible case AGAINST the trade "
    "implied by this signal. Cite concrete risks an Indian-market specialist "
    "would catch: ASM/GSM stage, F&O ban, promoter pledge, persistent FII "
    "selling, sector index breakdown, USFDA observation, NPA cycle worries, "
    "ADR-cash divergence, recent QIP dilution, audit qualifications, hostile "
    "regulatory orders, monsoon/festive demand miss. Be honest about "
    "conviction — if the bear case is thin, say so via low confidence. "
    "Respond with strict JSON only — no prose, no markdown."
)

_JUDGE_PROMPT = (
    _india_briefing(
        include_flow=True, include_sector=False,
        include_red_flags=False, include_seasonality=False,
    )
    + "\n\nROLE: You are the chair of an Indian-equities trading committee. "
    "You have read the bull case and bear case for the same candidate "
    "signal. Decide which side is more persuasive given the evidence each "
    "cites and the Indian-market context above. If both sides cite "
    "high-quality, mutually-contradicting evidence (or both lean on thin "
    "evidence), return 'inconclusive'. Your calibrated_confidence MUST "
    "reflect the *quality and uniqueness of the winning case's evidence*, "
    "not just the existence of an opinion. Respond with strict JSON only — "
    "no prose, no markdown."
)


def is_debate_enabled(settings: dict[str, Any]) -> bool:
    """Read user-facing toggle. Default off — debate adds 3× LLM cost."""
    val = str(settings.get("debate_enabled", "false")).strip().lower()
    return val in ("true", "1", "yes", "on")


def _argument_prompt(side: str, signal: dict) -> str:
    return (
        f"SIGNAL UNDER REVIEW (JSON):\n"
        f"{json.dumps({k: v for k, v in signal.items() if k in {'id', 'symbol', 'signal_type', 'direction', 'strength', 'reason', 'current_price'}}, separators=(',', ':'))}\n\n"
        f"Respond with strict JSON only — no prose, no markdown:\n"
        f'{{"side":"{side}","thesis":"<1-2 sentence core argument>",'
        f'"key_evidence":["fact 1","fact 2","fact 3"],'
        f'"confidence":0.0-1.0}}'
    )


def _judge_prompt(signal: dict, bull: DebateArgument, bear: DebateArgument) -> str:
    return (
        f"SIGNAL (JSON):\n{json.dumps({k: signal.get(k) for k in ['symbol','signal_type','direction','strength','reason']}, separators=(',', ':'))}\n\n"
        f"BULL CASE:\n{json.dumps(bull.model_dump(), separators=(',', ':'))}\n\n"
        f"BEAR CASE:\n{json.dumps(bear.model_dump(), separators=(',', ':'))}\n\n"
        f"Respond with strict JSON only — no prose, no markdown:\n"
        f'{{"winner":"bull|bear|inconclusive",'
        f'"synthesis":"<1-2 sentence verdict>",'
        f'"calibrated_confidence":0.0-1.0,'
        f'"rationale":"<why this side wins>"}}'
    )


async def _argue_one_side(
    side: str,
    signal: dict,
    settings: dict,
    *,
    provider: str,
    model: str,
    api_key: str,
    fallback: list,
) -> Optional[DebateArgument]:
    system_message = _BULL_PROMPT if side == "bull" else _BEAR_PROMPT
    try:
        raw = await call_llm(
            provider=provider, model=model, api_key=api_key,
            prompt=_argument_prompt(side, signal),
            system_message=system_message,
            fallback_chain=fallback,
            route="debate_arg",
            symbol=signal.get("symbol"),
            max_tokens=1024,
        )
    except Exception as e:
        logger.warning("debate %s side LLM call failed: %s", side, e)
        return None
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
        return DebateArgument.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as e:
        logger.debug("debate %s side parse failed: %s; raw=%.200s", side, e, raw)
        return None


async def debate_signal(
    signal: dict,
    settings: dict[str, Any],
) -> Optional[DebateResult]:
    """Run one signal through bull/bear/judge. Returns ``None`` on failure."""
    provider = settings.get("llm_provider", "openai")
    model = settings.get("llm_model", "gpt-5-mini")
    api_key = _get_api_key(settings, provider)
    if not api_key:
        return None
    fallback = _build_fallback_chain(settings, provider)

    # Bull + bear in parallel — they're independent.
    bull, bear = await asyncio.gather(
        _argue_one_side("bull", signal, settings,
                        provider=provider, model=model, api_key=api_key, fallback=fallback),
        _argue_one_side("bear", signal, settings,
                        provider=provider, model=model, api_key=api_key, fallback=fallback),
    )
    if bull is None or bear is None:
        # Can't run the judge without both sides — fail open.
        return None

    # Judge depends on both sides.
    try:
        raw = await call_llm(
            provider=provider, model=model, api_key=api_key,
            prompt=_judge_prompt(signal, bull, bear),
            system_message=_JUDGE_PROMPT,
            fallback_chain=fallback,
            route="debate_judge",
            symbol=signal.get("symbol"),
            max_tokens=1024,
        )
        if not raw or not raw.strip():
            return None
        verdict = DebateVerdict.model_validate(json.loads(raw))
    except (ValidationError, json.JSONDecodeError, Exception) as e:
        logger.warning("debate judge failed for %s: %s", signal.get("symbol"), e)
        return None

    return DebateResult(
        signal_id=signal["id"], bull=bull, bear=bear, verdict=verdict,
    )


async def debate_top_signals(
    signals: list[dict],
    settings: dict[str, Any],
    *,
    top_n: int = _DEBATE_TOP_N,
) -> dict[str, DebateResult]:
    """Debate the ``top_n`` highest-strength directional signals.

    Returns a ``{signal_id: DebateResult}`` map. Failures (silently) skip
    that signal — caller checks coverage before relying on output.
    """
    if not signals or not is_debate_enabled(settings):
        return {}
    candidates = [
        s for s in signals
        if s.get("direction") in ("bullish", "bearish")
        and s.get("strength", 0) >= 7
    ]
    candidates.sort(key=lambda s: s.get("strength", 0), reverse=True)
    candidates = candidates[:top_n]
    if not candidates:
        return {}

    results = await asyncio.gather(
        *[debate_signal(s, settings) for s in candidates],
        return_exceptions=True,
    )
    out: dict[str, DebateResult] = {}
    for sig, res in zip(candidates, results):
        if isinstance(res, DebateResult):
            out[sig["id"]] = res
    if out:
        wins = {"bull": 0, "bear": 0, "inconclusive": 0}
        for r in out.values():
            wins[r.verdict.winner] = wins.get(r.verdict.winner, 0) + 1
        logger.info(
            "debate: %d signals → bull=%d, bear=%d, inconclusive=%d",
            len(out), wins.get("bull", 0), wins.get("bear", 0), wins.get("inconclusive", 0),
        )
    return out

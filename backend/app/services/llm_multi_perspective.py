"""Multi-perspective LLM analyst.

Four specialist agents read the same signal from different angles, each
emitting structured Pydantic output (no free text). A synthesiser then
combines them with explicit weights to produce a single aggregate score
and a two-sentence summary the UI can surface.

Perspectives (weights tunable):
- **Technical** (0.30): chart structure, momentum, volume, regime fit
- **Fundamental** (0.30): earnings, balance sheet, valuation, growth
- **Sentiment** (0.20): news, analyst revisions, corporate actions
- **Macro/Regime** (0.20): sector winds, FII/DII flows, broader market

Cost: 5 LLM calls per analysed signal (4 perspectives + 1 synthesiser).
The orchestrator caps this to the *top N high-conviction* signals so a
busy scan stays under a tight budget.

Failure semantics: if any perspective parse-fails, it's dropped from the
weighted aggregate (weights renormalise across surviving perspectives).
The synthesiser still runs as long as ≥ 2 perspectives produced output.
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
    MultiPerspectiveAnalysis,
    PerspectiveAnalysis,
)

logger = logging.getLogger(__name__)

# Per-perspective weight. Sum should be 1.0 but the synthesiser
# renormalises across surviving perspectives so dropouts don't bias.
PERSPECTIVE_WEIGHTS: dict[str, float] = {
    "technical": 0.30,
    "fundamental": 0.30,
    "sentiment": 0.20,
    "macro": 0.20,
}

_DEFAULT_TOP_N = 5


# ─────────────────────────────────────────────────────────────────────────
# System prompts — one per perspective
# ─────────────────────────────────────────────────────────────────────────

from app.services.llm_india_context import briefing as _india_briefing


_TECH_BRIEF = _india_briefing(
    include_flow=True, include_sector=False,
    include_red_flags=True, include_seasonality=False,
)
_FUND_BRIEF = _india_briefing(
    include_flow=False, include_sector=True,
    include_red_flags=True, include_seasonality=False,
)
_SENT_BRIEF = _india_briefing(
    include_flow=True, include_sector=False,
    include_red_flags=True, include_seasonality=True,
)
_MACRO_BRIEF = _india_briefing(
    include_flow=True, include_sector=True,
    include_red_flags=False, include_seasonality=True,
)


_TECH_SYS = (
    _TECH_BRIEF
    + "\n\nROLE: You are the technical analyst on an Indian-equities trading "
    "desk. Judge ONLY the technical setup against the rule direction: "
    "intraday + daily structure, multi-timeframe MA alignment (esp. vs "
    "Nifty/sector index), RSI/MACD divergences, ADX regime fit, volume + "
    "delivery % confirmation, support/resistance proximity, gap behaviour, "
    "tick-size-rounded entry/stop levels. Penalise illiquid scrips, T2T/Z "
    "group, and circuit-breaker risk. Score -1.0 (strongly contradicts rule "
    "direction) to +1.0 (strongly supports). Return strict JSON only — no "
    "prose, no markdown."
)

_FUND_SYS = (
    _FUND_BRIEF
    + "\n\nROLE: You are the fundamental analyst on an Indian-equities trading "
    "desk. Judge ONLY the fundamentals against the rule direction: earnings "
    "trajectory (recent QoQ/YoY), guidance changes, sector-relative valuation "
    "(P/E vs sector median, P/BV for banks), ROE/ROCE sustainability, "
    "debt-to-equity (with sector lens — banks are different), promoter "
    "pledge level, recent QIP/preferential dilution, audit qualifications. "
    "Banks: NIM, CASA, slippage. Pharma: USFDA + NLEM. IT: USD-INR + deal "
    "TCV. Auto: monthly volumes + inventory channel. Score -1.0 (broken "
    "fundamentals contradict the trade) to +1.0 (strong fundamentals "
    "support). Return strict JSON only — no prose, no markdown."
)

_SENT_SYS = (
    _SENT_BRIEF
    + "\n\nROLE: You are the sentiment & news analyst on an Indian-equities "
    "trading desk. Judge ONLY narrative against the rule direction: recent "
    "news flow (corporate announcements, regulator orders, earnings calls), "
    "bulk/block-deal disclosures, analyst rating revisions on the street, "
    "social/forum chatter for retail-favourite names, promoter "
    "buy/sell/pledge updates, results-day delivery % skew. Surface SEBI/RBI "
    "actions and management departures as hard negatives. Score -1.0 "
    "(deteriorating narrative) to +1.0 (improving narrative). Return strict "
    "JSON only — no prose, no markdown."
)

_MACRO_SYS = (
    _MACRO_BRIEF
    + "\n\nROLE: You are the macro/sector strategist on an Indian-equities "
    "trading desk. Judge ONLY the macro + sector context against the rule "
    "direction: parent sector index trend (Nifty Bank for banks, Nifty IT "
    "for IT etc.), FII/DII flow direction, India VIX regime, USD/INR + "
    "Brent + LME levels insofar as they affect this stock's industry, "
    "RBI rate path, govt policy/Budget anchors, monsoon (when relevant), "
    "festive seasonality, election cycle bias. Score -1.0 (macro headwind) "
    "to +1.0 (macro tailwind). Return strict JSON only — no prose, no markdown."
)

_PERSPECTIVE_SYS = {
    "technical": _TECH_SYS,
    "fundamental": _FUND_SYS,
    "sentiment": _SENT_SYS,
    "macro": _MACRO_SYS,
}

_SYNTH_SYS = (
    _india_briefing(
        include_flow=True, include_sector=False,
        include_red_flags=False, include_seasonality=False,
    )
    + "\n\nROLE: You are the CIO of an Indian-equities fund. You have just "
    "received four specialist reports — technical, fundamental, sentiment, "
    "macro — on a single candidate signal. Write a TWO-sentence synthesis "
    "that explicitly names which desks agree, which disagree, and *why* "
    "(citing Indian-market specifics where they applied: FII direction, "
    "sector index, USFDA, promoter pledge, USD-INR, etc.). Pick the "
    "consensus tier. Return strict JSON only — no prose, no markdown."
)


# ─────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────

def is_multi_perspective_enabled(settings: dict[str, Any]) -> bool:
    """User-facing toggle. Off by default — adds 5× the LLM cost per
    analysed signal."""
    val = str(settings.get("multi_perspective_enabled", "false")).strip().lower()
    return val in ("true", "1", "yes", "on")


async def analyse_signal_multi_perspective(
    signal: dict,
    settings: dict[str, Any],
    *,
    fundamentals: Optional[dict] = None,
    market_ctx: Optional[dict] = None,
) -> Optional[MultiPerspectiveAnalysis]:
    """Run all four specialists in parallel + synthesise. None on failure.

    ``fundamentals`` and ``market_ctx`` are optional context — passing
    pre-fetched values avoids re-fetching on every call. The orchestrator
    has both cached.
    """
    provider = settings.get("llm_provider", "openai")
    model = settings.get("llm_model", "gpt-5-mini")
    api_key = _get_api_key(settings, provider)
    if not api_key:
        return None
    fallback = _build_fallback_chain(settings, provider)

    base_payload = {
        "id": signal.get("id"),
        "symbol": signal.get("symbol"),
        "signal_type": signal.get("signal_type"),
        "direction": signal.get("direction"),
        "strength": signal.get("strength"),
        "reason": (signal.get("reason") or "")[:200],
        "price": signal.get("current_price"),
        # Trimmed context payloads — keep token cost bounded.
        "fundamentals_summary": _fundamentals_brief(fundamentals),
        "market_context": _market_brief(market_ctx),
    }

    # Fire all four perspectives concurrently — they're independent.
    tasks = [
        _run_perspective(p, base_payload, provider, model, api_key, fallback)
        for p in PERSPECTIVE_WEIGHTS.keys()
    ]
    raw_results = await asyncio.gather(*tasks, return_exceptions=False)
    perspectives: list[PerspectiveAnalysis] = [
        r for r in raw_results if r is not None
    ]

    if len(perspectives) < 2:
        # Not enough survivors to synthesise meaningfully.
        logger.debug("multi-perspective: only %d perspectives survived for %s",
                     len(perspectives), signal.get("symbol"))
        return None

    # Weighted aggregate over surviving perspectives.
    total_weight = sum(PERSPECTIVE_WEIGHTS[p.perspective] for p in perspectives)
    if total_weight <= 0:
        return None
    aggregate = sum(
        p.score * PERSPECTIVE_WEIGHTS[p.perspective] for p in perspectives
    ) / total_weight

    # Synthesiser is given the agreed/disagreed reads + the math, and
    # asked to pick consensus + write two sentences.
    synth = await _run_synthesiser(
        base_payload, perspectives, aggregate,
        provider, model, api_key, fallback,
    )
    if synth is None:
        # Fall back to a deterministic synthesis so we still surface
        # *something* useful when the LLM call fails.
        synth_text = _fallback_synthesis(perspectives, aggregate)
        consensus = _consensus_tier(aggregate)
    else:
        synth_text, consensus = synth

    return MultiPerspectiveAnalysis(
        signal_id=signal["id"],
        perspectives=perspectives,
        aggregate_score=round(aggregate, 4),
        consensus=consensus,
        synthesis=synth_text,
    )


async def analyse_top_signals(
    signals: list[dict],
    settings: dict[str, Any],
    *,
    top_n: int = _DEFAULT_TOP_N,
) -> dict[str, MultiPerspectiveAnalysis]:
    """Run the multi-perspective analyst on the top N directional signals
    by strength. Returns a ``{signal_id: analysis}`` map."""
    if not signals or not is_multi_perspective_enabled(settings):
        return {}
    candidates = [
        s for s in signals
        if s.get("direction") in ("bullish", "bearish")
        and s.get("strength", 0) >= 6
    ]
    candidates.sort(key=lambda s: s.get("strength", 0), reverse=True)
    candidates = candidates[:top_n]
    if not candidates:
        return {}

    # Limit concurrency to avoid hammering the provider — 2 in flight at a time.
    sem = asyncio.Semaphore(2)

    async def bounded(sig: dict) -> tuple[str, Optional[MultiPerspectiveAnalysis]]:
        async with sem:
            return sig["id"], await analyse_signal_multi_perspective(sig, settings)

    results = await asyncio.gather(*[bounded(s) for s in candidates], return_exceptions=False)
    out: dict[str, MultiPerspectiveAnalysis] = {}
    for sig_id, mpa in results:
        if isinstance(mpa, MultiPerspectiveAnalysis):
            out[sig_id] = mpa
    if out:
        avg = sum(m.aggregate_score for m in out.values()) / len(out)
        logger.info(
            "multi-perspective: analysed %d signals, mean aggregate=%.2f",
            len(out), avg,
        )
    return out


# ─────────────────────────────────────────────────────────────────────────
# Helpers — per-perspective call + synthesiser + deterministic fallbacks
# ─────────────────────────────────────────────────────────────────────────

async def _run_perspective(
    perspective: str,
    payload: dict,
    provider: str,
    model: str,
    api_key: str,
    fallback: list,
) -> Optional[PerspectiveAnalysis]:
    prompt = (
        f"CANDIDATE SIGNAL (JSON):\n"
        f"{json.dumps(payload, separators=(',', ':'))}\n\n"
        f"Respond with strict JSON of the form:\n"
        f'{{"perspective":"{perspective}",'
        f'"score":-1.0..1.0,'
        f'"confidence":0.0..1.0,'
        f'"summary":"<≤2 sentences>",'
        f'"key_drivers":["fact1","fact2","fact3"],'
        f'"red_flags":["risk1","risk2"]}}'
    )
    try:
        try:
            from app.services.market_snapshot import get_live_briefing_block
            prompt = f"{await get_live_briefing_block()}\n\n{prompt}"
        except Exception:
            pass
        raw = await call_llm(
            provider=provider, model=model, api_key=api_key,
            prompt=prompt,
            system_message=_PERSPECTIVE_SYS[perspective],
            fallback_chain=fallback,
            route=f"perspective_{perspective}",
            symbol=payload.get("symbol"),
            max_tokens=1024,
        )
    except Exception as e:
        logger.debug("perspective %s call failed: %s", perspective, e)
        return None
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
        # Force the perspective field — model sometimes drops or renames it.
        data["perspective"] = perspective
        return PerspectiveAnalysis.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as e:
        logger.debug("perspective %s parse failed: %s; raw=%.200s",
                     perspective, e, raw)
        return None


async def _run_synthesiser(
    payload: dict,
    perspectives: list[PerspectiveAnalysis],
    aggregate: float,
    provider: str,
    model: str,
    api_key: str,
    fallback: list,
) -> Optional[tuple[str, str]]:
    perspectives_payload = [
        {"perspective": p.perspective, "score": p.score, "summary": p.summary}
        for p in perspectives
    ]
    prompt = (
        f"CANDIDATE SIGNAL (JSON):\n"
        f"{json.dumps({k: payload.get(k) for k in ['symbol', 'signal_type', 'direction', 'strength']}, separators=(',', ':'))}\n\n"
        f"SPECIALIST READS:\n{json.dumps(perspectives_payload, separators=(',', ':'))}\n\n"
        f"WEIGHTED AGGREGATE: {aggregate:.3f}\n\n"
        f"Respond with strict JSON only:\n"
        f'{{"synthesis":"<2 sentences naming agreement and disagreement>",'
        f'"consensus":"strong_confirm|confirm|mixed|contradict|strong_contradict"}}'
    )
    try:
        try:
            from app.services.market_snapshot import get_live_briefing_block
            prompt = f"{await get_live_briefing_block()}\n\n{prompt}"
        except Exception:
            pass
        raw = await call_llm(
            provider=provider, model=model, api_key=api_key,
            prompt=prompt,
            system_message=_SYNTH_SYS,
            fallback_chain=fallback,
            route="perspective_synth",
            symbol=payload.get("symbol"),
            max_tokens=512,
        )
        if not raw or not raw.strip():
            return None
        data = json.loads(raw)
        synthesis = str(data.get("synthesis", ""))[:400]
        consensus = str(data.get("consensus", "mixed"))
        if consensus not in {
            "strong_confirm", "confirm", "mixed", "contradict", "strong_contradict",
        }:
            consensus = _consensus_tier(aggregate)
        return synthesis, consensus
    except Exception as e:
        logger.debug("synthesiser call failed: %s", e)
        return None


def _consensus_tier(aggregate: float) -> str:
    """Deterministic mapping from the weighted aggregate to a tier label."""
    if aggregate >= 0.6:
        return "strong_confirm"
    if aggregate >= 0.2:
        return "confirm"
    if aggregate <= -0.6:
        return "strong_contradict"
    if aggregate <= -0.2:
        return "contradict"
    return "mixed"


def _fallback_synthesis(
    perspectives: list[PerspectiveAnalysis], aggregate: float,
) -> str:
    """Two-sentence deterministic synthesis when the LLM synth call fails."""
    confirm = [p.perspective for p in perspectives if p.score > 0.1]
    contra = [p.perspective for p in perspectives if p.score < -0.1]
    a = f"Weighted aggregate {aggregate:+.2f} across {len(perspectives)} perspectives."
    if confirm and contra:
        b = (
            f"{', '.join(confirm)} confirm the rule direction; "
            f"{', '.join(contra)} push back."
        )
    elif confirm:
        b = f"{', '.join(confirm)} confirm the rule direction with no contradictions."
    elif contra:
        b = f"{', '.join(contra)} contradict the rule direction."
    else:
        b = "No perspective took a strong side — treat as low-conviction."
    return f"{a} {b}"


def _fundamentals_brief(f: Optional[dict]) -> dict:
    """Compress fundamentals into a token-cheap payload for the LLM."""
    if not f:
        return {}
    return {
        "score": f.get("fundamental_score"),
        "pe": (f.get("valuation") or {}).get("pe"),
        "pb": (f.get("valuation") or {}).get("pb"),
        "roe": (f.get("profitability") or {}).get("roe"),
        "d_to_e": (f.get("financial_health") or {}).get("debt_to_equity"),
        "rev_growth": (f.get("growth") or {}).get("revenue_growth"),
        "earnings_growth": (f.get("growth") or {}).get("earnings_growth"),
    }


def _market_brief(m: Optional[dict]) -> dict:
    """Compress market context payload — keep token budget tight."""
    if not m:
        return {}
    regime = (m.get("market_regime") or {}).get("regime")
    fii = (m.get("fii_dii") or {}).get("fii_net")
    dii = (m.get("fii_dii") or {}).get("dii_net")
    vix = m.get("india_vix")
    return {"regime": regime, "fii_net": fii, "dii_net": dii, "india_vix": vix}

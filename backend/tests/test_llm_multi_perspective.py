"""Tests for the multi-perspective LLM analyst."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.llm_multi_perspective import (
    PERSPECTIVE_WEIGHTS,
    _consensus_tier,
    _fallback_synthesis,
    analyse_signal_multi_perspective,
    analyse_top_signals,
    is_multi_perspective_enabled,
)
from app.services.llm_schemas import (
    MultiPerspectiveAnalysis,
    PerspectiveAnalysis,
)


def _signal(id="s1", strength=8, direction="bullish") -> dict:
    return {
        "id": id, "symbol": "RELIANCE", "signal_type": "breakout",
        "direction": direction, "strength": strength,
        "reason": "above R1 on 3x volume", "current_price": 2400,
    }


def _perspective_payload(perspective: str, score: float = 0.5) -> str:
    return json.dumps({
        "perspective": perspective,
        "score": score,
        "confidence": 0.7,
        "summary": f"{perspective} read is supportive",
        "key_drivers": ["d1", "d2"],
        "red_flags": [],
    })


def _synth_payload() -> str:
    return json.dumps({
        "synthesis": "All four desks confirm the breakout.",
        "consensus": "strong_confirm",
    })


# ── Toggle ──────────────────────────────────────────────────────────────

def test_toggle_defaults_off():
    assert is_multi_perspective_enabled({}) is False


def test_toggle_truthy_strings():
    for v in ("true", "1", "yes", "on"):
        assert is_multi_perspective_enabled({"multi_perspective_enabled": v}) is True


# ── Consensus tier helper ────────────────────────────────────────────────

def test_consensus_tier_thresholds():
    assert _consensus_tier(0.8) == "strong_confirm"
    assert _consensus_tier(0.3) == "confirm"
    assert _consensus_tier(0.05) == "mixed"
    assert _consensus_tier(-0.3) == "contradict"
    assert _consensus_tier(-0.8) == "strong_contradict"


# ── Fallback synthesis (deterministic, no LLM) ──────────────────────────

def test_fallback_synthesis_handles_mixed_perspectives():
    perspectives = [
        PerspectiveAnalysis(perspective="technical", score=0.5, confidence=0.7, summary="x"),
        PerspectiveAnalysis(perspective="fundamental", score=-0.4, confidence=0.7, summary="y"),
    ]
    out = _fallback_synthesis(perspectives, aggregate=0.05)
    assert "technical" in out and "fundamental" in out


def test_fallback_synthesis_handles_unanimous_perspectives():
    perspectives = [
        PerspectiveAnalysis(perspective="technical", score=0.5, confidence=0.7, summary="x"),
        PerspectiveAnalysis(perspective="macro", score=0.4, confidence=0.7, summary="y"),
    ]
    out = _fallback_synthesis(perspectives, aggregate=0.45)
    assert "no contradictions" in out


# ── Full orchestration (mocked LLM) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_analyse_signal_returns_full_object():
    """All 4 perspectives succeed → MultiPerspectiveAnalysis with synth."""
    settings = {"llm_provider": "openai", "openai_api_key": "k"}
    # Order: technical, fundamental, sentiment, macro (parallel call order
    # is not deterministic, so route through the perspective field instead).
    call_order = list(PERSPECTIVE_WEIGHTS.keys()) + ["synth"]
    responses = [_perspective_payload(p, 0.5) for p in PERSPECTIVE_WEIGHTS] + [_synth_payload()]
    seq = list(zip(call_order, responses))

    async def fake_call(**kw):
        # Pop by route prefix to keep parallel ordering robust.
        route = kw.get("route", "")
        if route.startswith("perspective_synth"):
            return _synth_payload()
        if route.startswith("perspective_"):
            persp = route.split("_", 1)[1]
            return _perspective_payload(persp, 0.5)
        raise AssertionError(f"unknown route {route}")

    with patch("app.services.llm_multi_perspective.call_llm", new=AsyncMock(side_effect=fake_call)):
        result = await analyse_signal_multi_perspective(_signal(), settings)

    assert isinstance(result, MultiPerspectiveAnalysis)
    assert len(result.perspectives) == 4
    # Aggregate ≈ 0.5 (all four scored +0.5)
    assert abs(result.aggregate_score - 0.5) < 1e-6
    assert result.consensus == "strong_confirm"


@pytest.mark.asyncio
async def test_analyse_signal_returns_none_without_api_key():
    result = await analyse_signal_multi_perspective(
        _signal(), settings={"llm_provider": "openai"},
    )
    assert result is None


@pytest.mark.asyncio
async def test_analyse_signal_drops_failed_perspectives():
    """Two perspectives fail → still synthesise from surviving two."""
    settings = {"llm_provider": "openai", "openai_api_key": "k"}

    async def fake_call(**kw):
        route = kw.get("route", "")
        if route in ("perspective_technical", "perspective_fundamental"):
            return _perspective_payload(route.split("_", 1)[1], 0.4)
        if route == "perspective_synth":
            return _synth_payload()
        # Sentiment + macro fail to return anything → dropped.
        return ""

    with patch("app.services.llm_multi_perspective.call_llm", new=AsyncMock(side_effect=fake_call)):
        result = await analyse_signal_multi_perspective(_signal(), settings)

    assert result is not None
    assert len(result.perspectives) == 2


@pytest.mark.asyncio
async def test_analyse_signal_returns_none_when_fewer_than_two_perspectives():
    settings = {"llm_provider": "openai", "openai_api_key": "k"}

    async def fake_call(**kw):
        route = kw.get("route", "")
        if route == "perspective_technical":
            return _perspective_payload("technical", 0.5)
        return ""   # others all fail

    with patch("app.services.llm_multi_perspective.call_llm", new=AsyncMock(side_effect=fake_call)):
        result = await analyse_signal_multi_perspective(_signal(), settings)
    assert result is None


@pytest.mark.asyncio
async def test_synth_falls_back_to_deterministic_when_synth_call_fails():
    settings = {"llm_provider": "openai", "openai_api_key": "k"}

    async def fake_call(**kw):
        route = kw.get("route", "")
        if route.startswith("perspective_synth"):
            raise RuntimeError("synth provider down")
        if route.startswith("perspective_"):
            return _perspective_payload(route.split("_", 1)[1], 0.5)
        return ""

    with patch("app.services.llm_multi_perspective.call_llm", new=AsyncMock(side_effect=fake_call)):
        result = await analyse_signal_multi_perspective(_signal(), settings)
    assert result is not None
    assert "perspectives" in result.synthesis.lower() or "confirm" in result.synthesis.lower()
    # All four perspectives at +0.5 ⇒ aggregate 0.5 → "confirm" tier (≥0.6 for strong).
    assert result.consensus == "confirm"


@pytest.mark.asyncio
async def test_analyse_top_signals_off_returns_empty():
    out = await analyse_top_signals(
        [_signal()], settings={"multi_perspective_enabled": "false"},
    )
    assert out == {}


@pytest.mark.asyncio
async def test_analyse_top_signals_caps_at_top_n():
    settings = {
        "llm_provider": "openai", "openai_api_key": "k",
        "multi_perspective_enabled": "true",
    }
    signals = [_signal(id=f"s{i}", strength=10 - i) for i in range(8)]

    async def fake_analyse(sig, settings, **kw):
        return MultiPerspectiveAnalysis(
            signal_id=sig["id"],
            perspectives=[
                PerspectiveAnalysis(perspective="technical", score=0.5, confidence=0.7, summary="x"),
                PerspectiveAnalysis(perspective="fundamental", score=0.4, confidence=0.7, summary="y"),
            ],
            aggregate_score=0.45,
            consensus="confirm",
            synthesis="ok",
        )

    with patch("app.services.llm_multi_perspective.analyse_signal_multi_perspective",
               new=AsyncMock(side_effect=fake_analyse)):
        out = await analyse_top_signals(signals, settings, top_n=5)
    assert len(out) == 5
    # Strongest 5 by strength: s0..s4
    assert set(out.keys()) == {f"s{i}" for i in range(5)}

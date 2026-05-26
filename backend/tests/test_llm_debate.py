"""Tests for the bull/bear/judge debate loop.

LLM calls are patched. We test orchestration: parallel argue → judge
synthesis, top-N filtering, fail-open behavior, schema enforcement.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.services.llm_debate import (
    debate_signal,
    debate_top_signals,
    is_debate_enabled,
)
from app.services.llm_schemas import DebateArgument, DebateResult, DebateVerdict


def _signal(id="s1", direction="bullish", strength=8) -> dict:
    return {
        "id": id,
        "symbol": "RELIANCE",
        "signal_type": "rsi_extreme",
        "direction": direction,
        "strength": strength,
        "reason": "RSI overbought at 78",
        "current_price": 2400,
    }


def _bull_payload(conf=0.7) -> str:
    return json.dumps({
        "side": "bull",
        "thesis": "Strong momentum + volume confirms continuation",
        "key_evidence": ["volume 3× avg", "above 200dma"],
        "confidence": conf,
    })


def _bear_payload(conf=0.6) -> str:
    return json.dumps({
        "side": "bear",
        "thesis": "RSI extreme typically reverts; high-vol context fading",
        "key_evidence": ["RSI 78 >70 threshold", "ADX falling"],
        "confidence": conf,
    })


def _judge_payload(winner="bull") -> str:
    return json.dumps({
        "winner": winner,
        "synthesis": "Bull case backed by independently observable volume",
        "calibrated_confidence": 0.65,
        "rationale": "Volume confirms thesis stronger than RSI reversal heuristic",
    })


def test_is_debate_enabled_defaults_off():
    assert is_debate_enabled({}) is False
    assert is_debate_enabled({"debate_enabled": "false"}) is False


def test_is_debate_enabled_truthy_strings():
    for v in ("true", "1", "yes", "on", "True"):
        assert is_debate_enabled({"debate_enabled": v}) is True


@pytest.mark.asyncio
async def test_debate_signal_returns_result_on_clean_payloads():
    settings = {"llm_provider": "openai", "openai_api_key": "k", "debate_enabled": "true"}
    raw_responses = [_bull_payload(), _bear_payload(), _judge_payload("bull")]

    async def fake_call(**kw):
        return raw_responses.pop(0)

    with patch("app.services.llm_debate.call_llm", new=AsyncMock(side_effect=fake_call)):
        result = await debate_signal(_signal(), settings)

    assert isinstance(result, DebateResult)
    assert result.signal_id == "s1"
    assert isinstance(result.bull, DebateArgument)
    assert isinstance(result.bear, DebateArgument)
    assert isinstance(result.verdict, DebateVerdict)
    assert result.verdict.winner == "bull"


@pytest.mark.asyncio
async def test_debate_signal_returns_none_without_api_key():
    result = await debate_signal(_signal(), settings={"llm_provider": "openai"})
    assert result is None


@pytest.mark.asyncio
async def test_debate_signal_returns_none_when_bull_call_fails():
    settings = {"llm_provider": "openai", "openai_api_key": "k"}

    async def fake_call(**kw):
        raise RuntimeError("provider down")

    with patch("app.services.llm_debate.call_llm", new=AsyncMock(side_effect=fake_call)):
        result = await debate_signal(_signal(), settings)
    assert result is None


@pytest.mark.asyncio
async def test_debate_signal_returns_none_on_bad_judge_schema():
    settings = {"llm_provider": "openai", "openai_api_key": "k"}
    # winner field missing → ValidationError on judge
    bad_judge = json.dumps({"synthesis": "ok", "calibrated_confidence": 0.5, "rationale": "x"})
    raw = [_bull_payload(), _bear_payload(), bad_judge]

    async def fake_call(**kw):
        return raw.pop(0)

    with patch("app.services.llm_debate.call_llm", new=AsyncMock(side_effect=fake_call)):
        result = await debate_signal(_signal(), settings)
    assert result is None


@pytest.mark.asyncio
async def test_debate_top_signals_off_returns_empty():
    out = await debate_top_signals([_signal()], settings={"debate_enabled": "false"})
    assert out == {}


@pytest.mark.asyncio
async def test_debate_top_signals_caps_at_top_n():
    settings = {
        "llm_provider": "openai", "openai_api_key": "k",
        "debate_enabled": "true",
    }
    signals = [_signal(id=f"s{i}", strength=10 - i) for i in range(6)]

    # Stub debate_signal directly — we're testing the top-N filter, not
    # per-call mechanics (covered elsewhere). Using call_llm with parallel
    # gather makes response ordering non-deterministic across signals.
    async def fake_debate_signal(signal: dict, settings: Any) -> DebateResult:
        return DebateResult(
            signal_id=signal["id"],
            bull=DebateArgument(side="bull", thesis="t", key_evidence=[], confidence=0.7),
            bear=DebateArgument(side="bear", thesis="t", key_evidence=[], confidence=0.6),
            verdict=DebateVerdict(
                winner="bull", synthesis="x",
                calibrated_confidence=0.65, rationale="y",
            ),
        )

    with patch("app.services.llm_debate.debate_signal", new=AsyncMock(side_effect=fake_debate_signal)):
        out = await debate_top_signals(signals, settings, top_n=3)

    assert len(out) == 3
    # Strongest 3 (s0,s1,s2) selected.
    assert set(out.keys()) == {"s0", "s1", "s2"}


@pytest.mark.asyncio
async def test_debate_top_signals_filters_low_strength_and_neutral():
    settings = {
        "llm_provider": "openai", "openai_api_key": "k",
        "debate_enabled": "true",
    }
    signals = [
        _signal(id="weak", strength=3),
        _signal(id="neutral", direction="neutral", strength=10),
    ]

    async def fake_call(**kw):
        raise AssertionError("LLM should not be called")

    with patch("app.services.llm_debate.call_llm", new=AsyncMock(side_effect=fake_call)):
        out = await debate_top_signals(signals, settings)
    assert out == {}

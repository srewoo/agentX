"""Unit tests for the Layer-2 LLM signal judge.

Tests cover the pure logic — prompt build, response parse, fail-open, and the
batched-cap behaviour. Provider calls are mocked; no network is touched.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.llm_signal_judge import (
    JudgeVerdict,
    _build_prompt,
    _parse_response,
    is_enabled,
    judge_signals,
)


def _cand(sig_id: str, symbol: str = "RELIANCE", strength: int = 7) -> dict:
    return {
        "id": sig_id,
        "symbol": symbol,
        "signal_type": "breakout",
        "direction": "bullish",
        "strength": strength,
        "reason": "Close above 50d MA on 2x volume",
        "current_price": 2500.0,
    }


def test_is_enabled_default_off():
    assert is_enabled({}) is False
    assert is_enabled({"llm_judging_enabled": "false"}) is False
    assert is_enabled({"llm_judging_enabled": "true"}) is True
    assert is_enabled({"llm_judging_enabled": "1"}) is True
    assert is_enabled({"llm_judging_enabled": "ON"}) is True


def test_build_prompt_contains_ids_and_compact_json():
    prompt = _build_prompt([_cand("sig-1"), _cand("sig-2", symbol="TCS")])
    assert "sig-1" in prompt
    assert "sig-2" in prompt
    assert "TCS" in prompt
    # Compact separators — no spaces after commas in the payload JSON.
    assert ", " not in prompt.split("CANDIDATES (JSON):")[1].split("\n")[0]


def test_parse_response_happy_path():
    raw = (
        '{"verdicts":['
        '{"id":"a","verdict":"keep","reason":"strong trend"},'
        '{"id":"b","verdict":"drop","reason":"counter-trend in bear regime"}'
        ']}'
    )
    out = _parse_response(raw, expected_ids={"a", "b"})
    assert set(out.keys()) == {"a", "b"}
    assert out["a"].verdict == "keep"
    assert out["b"].verdict == "drop"


def test_parse_response_strips_markdown_fence():
    raw = "```json\n{\"verdicts\":[{\"id\":\"a\",\"verdict\":\"keep\",\"reason\":\"ok\"}]}\n```"
    out = _parse_response(raw, expected_ids={"a"})
    assert "a" in out


def test_parse_response_drops_unknown_ids():
    raw = '{"verdicts":[{"id":"ghost","verdict":"keep","reason":"x"}]}'
    out = _parse_response(raw, expected_ids={"a"})
    assert out == {}


def test_parse_response_drops_invalid_verdict():
    raw = '{"verdicts":[{"id":"a","verdict":"buy","reason":"nope"}]}'
    out = _parse_response(raw, expected_ids={"a"})
    assert out == {}  # 'buy' is not in the pattern → dropped


def test_judge_verdict_model_enforces_pattern():
    with pytest.raises(Exception):
        JudgeVerdict(id="x", verdict="maybe", reason="hmm")


@pytest.mark.asyncio
async def test_judge_signals_no_api_key_returns_empty():
    out = await judge_signals([_cand("a")], settings={"llm_provider": "gemini"})
    assert out == {}


@pytest.mark.asyncio
async def test_judge_signals_no_candidates_returns_empty():
    out = await judge_signals(
        [], settings={"llm_provider": "gemini", "gemini_api_key": "k"}
    )
    assert out == {}


@pytest.mark.asyncio
async def test_judge_signals_fail_open_on_llm_error():
    fake = AsyncMock(side_effect=RuntimeError("provider exploded"))
    with patch("app.services.llm_signal_judge.call_llm", fake):
        out = await judge_signals(
            [_cand("a")],
            settings={"llm_provider": "gemini", "gemini_api_key": "k"},
        )
    assert out == {}


@pytest.mark.asyncio
async def test_judge_signals_fail_open_on_parse_error():
    fake = AsyncMock(return_value="not json at all")
    with patch("app.services.llm_signal_judge.call_llm", fake):
        out = await judge_signals(
            [_cand("a")],
            settings={"llm_provider": "gemini", "gemini_api_key": "k"},
        )
    assert out == {}


@pytest.mark.asyncio
async def test_judge_signals_happy_path():
    raw = (
        '{"verdicts":[{"id":"a","verdict":"downgrade","reason":"earnings tomorrow"}]}'
    )
    fake = AsyncMock(return_value=raw)
    with patch("app.services.llm_signal_judge.call_llm", fake):
        out = await judge_signals(
            [_cand("a")],
            settings={"llm_provider": "gemini", "gemini_api_key": "k"},
        )
    assert "a" in out
    assert out["a"].verdict == "downgrade"
    assert "earnings" in out["a"].reason


@pytest.mark.asyncio
async def test_judge_signals_caps_candidates_by_strength():
    # 50 candidates with ascending strength; cap is 40, top-strength should win.
    cands = [_cand(f"sig-{i}", strength=i % 10 + 1) for i in range(50)]
    captured: dict = {}

    async def _capture(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return '{"verdicts":[]}'

    with patch("app.services.llm_signal_judge.call_llm", _capture):
        await judge_signals(
            cands, settings={"llm_provider": "gemini", "gemini_api_key": "k"}
        )
    # Only top-40-by-strength sent — count ids in the prompt payload.
    sent_ids = [f"sig-{i}" for i in range(50) if f'"id":"sig-{i}"' in captured["prompt"]]
    assert len(sent_ids) == 40

from __future__ import annotations
"""Tests for app.services.llm_client — unified LLM dispatcher."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.llm_client import (
    call_llm,
    SUPPORTED_MODELS,
    _is_openai_reasoning,
    _validate_provider_model,
)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _make_openai_response(content: str) -> MagicMock:
    """Build a fake openai ChatCompletion response."""
    choice = MagicMock()
    choice.message.content = content
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def _make_gemini_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.text = text
    return resp


def _make_claude_response(text: str) -> MagicMock:
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


# ─────────────────────────────────────────────
# Model registry / validation
# ─────────────────────────────────────────────

class TestSupportedModels:
    def test_all_providers_present(self):
        assert set(SUPPORTED_MODELS.keys()) == {"openai", "gemini", "claude"}

    def test_openai_contains_gpt41(self):
        assert "gpt-4.1" in SUPPORTED_MODELS["openai"]

    def test_gemini_contains_25_flash(self):
        assert "gemini-2.5-flash" in SUPPORTED_MODELS["gemini"]

    def test_claude_contains_opus_46(self):
        assert "claude-opus-4-6" in SUPPORTED_MODELS["claude"]

    def test_claude_contains_sonnet_46(self):
        assert "claude-sonnet-4-6" in SUPPORTED_MODELS["claude"]

    def test_each_provider_has_models(self):
        for provider, models in SUPPORTED_MODELS.items():
            assert len(models) > 0, f"{provider} has no models"


class TestIsOpenAIReasoning:
    def test_o1_is_reasoning(self):
        assert _is_openai_reasoning("o1")

    def test_o3_is_reasoning(self):
        assert _is_openai_reasoning("o3")

    def test_o4_mini_is_reasoning(self):
        assert _is_openai_reasoning("o4-mini")

    def test_gpt4o_not_reasoning(self):
        assert not _is_openai_reasoning("gpt-4o")

    def test_gpt41_not_reasoning(self):
        assert not _is_openai_reasoning("gpt-4.1")

    def test_gpt41_mini_not_reasoning(self):
        assert not _is_openai_reasoning("gpt-4.1-mini")


class TestValidateProviderModel:
    def test_valid_openai_model(self):
        _validate_provider_model("openai", "gpt-4o")  # should not raise

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            _validate_provider_model("groq", "llama3")

    def test_unknown_model_raises(self):
        with pytest.raises(ValueError, match="Unknown model"):
            _validate_provider_model("openai", "gpt-99")


# ─────────────────────────────────────────────
# call_llm — missing API key
# ─────────────────────────────────────────────

class TestCallLlmMissingKey:
    @pytest.mark.asyncio
    async def test_empty_key_raises_value_error(self):
        with pytest.raises(ValueError, match="No API key"):
            await call_llm("openai", "gpt-4o", "", "hello")

    @pytest.mark.asyncio
    async def test_none_key_raises_value_error(self):
        with pytest.raises(ValueError, match="No API key"):
            await call_llm("openai", "gpt-4o", None, "hello")  # type: ignore


# ─────────────────────────────────────────────
# OpenAI dispatch
# ─────────────────────────────────────────────

class TestCallOpenAI:
    @pytest.mark.asyncio
    async def test_success_returns_text(self):
        fake_resp = _make_openai_response('{"result": "ok"}')
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=fake_resp)

        with patch("app.services.llm_client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = mock_client
            result = await call_llm("openai", "gpt-4o", "key123", "test prompt")

        assert result == '{"result": "ok"}'

    @pytest.mark.asyncio
    async def test_standard_model_uses_json_response_format(self):
        fake_resp = _make_openai_response("{}")
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=fake_resp)

        with patch("app.services.llm_client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = mock_client
            await call_llm("openai", "gpt-4o", "key123", "prompt")

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert call_kwargs.get("response_format") == {"type": "json_object"}

    @pytest.mark.asyncio
    async def test_reasoning_model_no_response_format(self):
        fake_resp = _make_openai_response("{}")
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=fake_resp)

        with patch("app.services.llm_client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = mock_client
            await call_llm("openai", "o3", "key123", "prompt")

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert "response_format" not in call_kwargs

    @pytest.mark.asyncio
    async def test_reasoning_model_uses_developer_role(self):
        fake_resp = _make_openai_response("{}")
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=fake_resp)

        with patch("app.services.llm_client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = mock_client
            await call_llm("openai", "o4-mini", "key123", "prompt", system_message="sys")

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        roles = [m["role"] for m in call_kwargs["messages"]]
        assert "developer" in roles
        assert "system" not in roles

    @pytest.mark.asyncio
    async def test_standard_model_uses_system_role(self):
        fake_resp = _make_openai_response("{}")
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=fake_resp)

        with patch("app.services.llm_client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = mock_client
            await call_llm("openai", "gpt-4.1", "key123", "prompt", system_message="sys")

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        roles = [m["role"] for m in call_kwargs["messages"]]
        assert "system" in roles
        assert "developer" not in roles

    @pytest.mark.asyncio
    async def test_sdk_exception_wraps_in_runtime_error(self):
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("API down"))

        with patch("app.services.llm_client.openai") as mock_openai:
            mock_openai.AsyncOpenAI.return_value = mock_client
            with pytest.raises(RuntimeError, match="OpenAI error"):
                await call_llm("openai", "gpt-4o", "key123", "prompt")


# ─────────────────────────────────────────────
# Gemini dispatch
# ─────────────────────────────────────────────

class TestCallGemini:
    @pytest.mark.asyncio
    async def test_success_returns_text(self):
        fake_resp = _make_gemini_response('{"result": "gemini"}')
        mock_model = MagicMock()
        mock_model.generate_content_async = AsyncMock(return_value=fake_resp)
        mock_genai = MagicMock()
        mock_genai.GenerativeModel.return_value = mock_model

        with patch("app.services.llm_client.genai", mock_genai):
            result = await call_llm("gemini", "gemini-2.0-flash", "gemini-key", "prompt")

        assert result == '{"result": "gemini"}'

    @pytest.mark.asyncio
    async def test_uses_native_async_not_executor(self):
        """Verify generate_content_async is called (not the sync variant)."""
        fake_resp = _make_gemini_response("{}")
        mock_model = MagicMock()
        mock_model.generate_content_async = AsyncMock(return_value=fake_resp)
        mock_genai = MagicMock()
        mock_genai.GenerativeModel.return_value = mock_model

        with patch("app.services.llm_client.genai", mock_genai):
            await call_llm("gemini", "gemini-2.5-flash", "key", "prompt")

        mock_model.generate_content_async.assert_called_once()
        mock_model.generate_content.assert_not_called()

    @pytest.mark.asyncio
    async def test_sdk_exception_wraps_in_runtime_error(self):
        mock_model = MagicMock()
        mock_model.generate_content_async = AsyncMock(side_effect=Exception("quota exceeded"))
        mock_genai = MagicMock()
        mock_genai.GenerativeModel.return_value = mock_model

        with patch("app.services.llm_client.genai", mock_genai):
            with pytest.raises(RuntimeError, match="Gemini error"):
                await call_llm("gemini", "gemini-2.0-flash", "key", "prompt")


# ─────────────────────────────────────────────
# Claude dispatch
# ─────────────────────────────────────────────

class TestCallClaude:
    @pytest.mark.asyncio
    async def test_success_returns_text(self):
        fake_resp = _make_claude_response('{"result": "claude"}')
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=fake_resp)

        with patch("app.services.llm_client.anthropic") as mock_anthropic:
            mock_anthropic.AsyncAnthropic.return_value = mock_client
            result = await call_llm("claude", "claude-sonnet-4-6", "claude-key", "prompt")

        assert result == '{"result": "claude"}'

    @pytest.mark.asyncio
    async def test_max_tokens_passed(self):
        fake_resp = _make_claude_response("{}")
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=fake_resp)

        with patch("app.services.llm_client.anthropic") as mock_anthropic:
            mock_anthropic.AsyncAnthropic.return_value = mock_client
            await call_llm("claude", "claude-haiku-4-5-20251001", "key", "prompt", max_tokens=512)

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["max_tokens"] == 512

    @pytest.mark.asyncio
    async def test_sdk_exception_wraps_in_runtime_error(self):
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=Exception("rate limited"))

        with patch("app.services.llm_client.anthropic") as mock_anthropic:
            mock_anthropic.AsyncAnthropic.return_value = mock_client
            with pytest.raises(RuntimeError, match="Claude error"):
                await call_llm("claude", "claude-sonnet-4-6", "key", "prompt")


# ─────────────────────────────────────────────
# Fallback chain
# ─────────────────────────────────────────────

class TestFallbackChain:
    @pytest.mark.asyncio
    async def test_primary_success_no_fallback_used(self):
        fake_resp = _make_claude_response('{"ok": 1}')
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=fake_resp)
        call_count = {"n": 0}

        async def counted_dispatch(prov, mod, key, sys, prompt, tokens):
            call_count["n"] += 1
            if prov == "claude":
                return '{"ok": 1}'
            raise RuntimeError("should not reach")

        with patch("app.services.llm_client._dispatch", side_effect=counted_dispatch):
            result = await call_llm(
                "claude", "claude-sonnet-4-6", "key",
                "prompt",
                fallback_chain=[("openai", "gpt-4.1-mini", "oai-key")],
            )

        assert call_count["n"] == 1
        assert result == '{"ok": 1}'

    @pytest.mark.asyncio
    async def test_primary_fails_fallback_succeeds(self):
        call_count = {"n": 0}

        async def dispatch_impl(prov, mod, key, sys, prompt, tokens):
            call_count["n"] += 1
            if prov == "claude":
                raise RuntimeError("claude down")
            return '{"fallback": true}'

        with patch("app.services.llm_client._dispatch", side_effect=dispatch_impl):
            result = await call_llm(
                "claude", "claude-sonnet-4-6", "claude-key",
                "prompt",
                fallback_chain=[("openai", "gpt-4.1-mini", "oai-key")],
            )

        assert call_count["n"] == 2
        assert '"fallback": true' in result

    @pytest.mark.asyncio
    async def test_all_fail_raises_runtime_error(self):
        async def always_fail(prov, mod, key, sys, prompt, tokens):
            raise RuntimeError(f"{prov} error")

        with patch("app.services.llm_client._dispatch", side_effect=always_fail):
            with pytest.raises(RuntimeError, match="All LLM providers exhausted"):
                await call_llm(
                    "claude", "claude-sonnet-4-6", "key",
                    "prompt",
                    fallback_chain=[("openai", "gpt-4.1-mini", "oai-key")],
                )

    @pytest.mark.asyncio
    async def test_no_fallback_chain_raises_on_primary_fail(self):
        async def always_fail(prov, mod, key, sys, prompt, tokens):
            raise RuntimeError("primary error")

        with patch("app.services.llm_client._dispatch", side_effect=always_fail):
            with pytest.raises(RuntimeError):
                await call_llm("claude", "claude-sonnet-4-6", "key", "prompt")

    @pytest.mark.asyncio
    async def test_fallback_entry_with_empty_key_skipped(self):
        """A fallback entry with an empty key should be skipped."""
        call_count = {"n": 0}

        async def dispatch_impl(prov, mod, key, sys, prompt, tokens):
            call_count["n"] += 1
            if prov == "openai" and key == "":
                raise AssertionError("should not dispatch with empty key")
            if prov == "claude":
                return '{"from": "claude"}'
            raise RuntimeError("unexpected")

        with patch("app.services.llm_client._dispatch", side_effect=dispatch_impl):
            result = await call_llm(
                "gemini", "gemini-2.0-flash", "gemini-key",
                "prompt",
                fallback_chain=[
                    ("openai", "gpt-4.1-mini", ""),       # empty key — skip
                    ("claude", "claude-sonnet-4-6", "ckey"),  # should be used
                ],
            )

        assert '"from": "claude"' in result

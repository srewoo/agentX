"""
Unified async LLM client supporting OpenAI, Google Gemini, and Anthropic Claude.
No third-party wrappers — direct SDK calls only.

Supports:
- Updated model lists (March 2026): Claude 4.x, GPT-4.1, Gemini 2.5
- Fallback chain: if primary provider fails, tries next configured provider
- Per-call retry with exponential backoff for transient errors (429, 5xx)
- OpenAI o-series: developer role, max_completion_tokens, no response_format
- OpenAI standard: response_format=json_object for reliable JSON output
- Gemini: native async (no run_in_executor)
- Per-provider token limits
"""
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Retry constants for individual provider calls ─────────────
_LLM_MAX_RETRIES = 2
_LLM_BASE_DELAY = 1.0  # seconds

# ─────────────────────────────────────────────
# Model registry
# ─────────────────────────────────────────────

SUPPORTED_MODELS: dict[str, list[str]] = {
    "openai": [
        "gpt-4.1",           # flagship, April 2025
        "gpt-4.1-mini",      # fast + cheap replacement for gpt-4o-mini
        "gpt-4o",            # still available
        "gpt-4o-mini",       # still available
        "o3",                # reasoning
        "o4-mini",           # reasoning, budget
    ],
    "gemini": [
        "gemini-2.5-pro",    # best as of March 2026
        "gemini-2.5-flash",  # fast + cheap
        "gemini-2.0-flash",  # current stable default
        "gemini-1.5-pro",    # fallback
    ],
    "claude": [
        "claude-opus-4-6",             # Claude Opus 4.6 — most capable
        "claude-sonnet-4-6",           # Claude Sonnet 4.6 — balanced
        "claude-haiku-4-5-20251001",   # Claude Haiku 4.5 — fast + cheap
        "claude-3-5-sonnet-20241022",  # legacy, kept for backward compat
    ],
}

# OpenAI reasoning model prefixes — these require different API parameters
_OPENAI_REASONING_PREFIXES = ("o1", "o3", "o4")

# Per-provider default max output tokens
_DEFAULT_MAX_TOKENS: dict[str, int] = {
    "openai": 2048,
    "gemini": 2048,
    "claude": 2048,
}


# ─────────────────────────────────────────────
# Public interface
# ─────────────────────────────────────────────

async def call_llm(
    provider: str,
    model: str,
    api_key: str,
    prompt: str,
    system_message: str = "You are an expert Indian stock market analyst. Respond with valid JSON only.",
    max_tokens: Optional[int] = None,
    fallback_chain: Optional[list[tuple[str, str, str]]] = None,
) -> str:
    """
    Call the specified LLM provider and return the raw text response.

    If the primary call fails and fallback_chain is provided, each entry
    (provider, model, api_key) is tried in order until one succeeds.

    Args:
        provider:       "openai" | "gemini" | "claude"
        model:          Model name (must be in SUPPORTED_MODELS[provider])
        api_key:        API key for the provider
        prompt:         User message / instruction
        system_message: System-level instruction
        max_tokens:     Override default output token limit
        fallback_chain: List of (provider, model, api_key) tried after primary failure

    Returns:
        Raw string response from the model.

    Raises:
        ValueError:     Unknown provider / model or missing key (not retried).
        RuntimeError:   All API calls failed after exhausting fallback chain.
    """
    if not api_key:
        raise ValueError("No API key configured. Please set your API key in Settings.")

    provider = provider.lower().strip()
    _validate_provider_model(provider, model)

    candidates: list[tuple[str, str, str]] = [(provider, model, api_key)]
    if fallback_chain:
        candidates.extend(fallback_chain)

    last_exc: Exception = RuntimeError("No providers attempted.")
    for prov, mod, key in candidates:
        if not key:
            continue
        try:
            tokens = max_tokens or _DEFAULT_MAX_TOKENS.get(prov, 2048)
            return await _dispatch(prov, mod, key, system_message, prompt, tokens)
        except ValueError:
            # Config errors (bad model/key format) — don't retry
            raise
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "LLM call failed for %s/%s, trying next in chain: %s",
                prov, mod, exc,
            )

    raise RuntimeError(
        f"All LLM providers exhausted. Last error: {last_exc}"
    ) from last_exc


# ─────────────────────────────────────────────
# Internal dispatch
# ─────────────────────────────────────────────

def _validate_provider_model(provider: str, model: str) -> None:
    if provider not in SUPPORTED_MODELS:
        raise ValueError(
            f"Unknown provider '{provider}'. Choose from: {list(SUPPORTED_MODELS)}"
        )
    if model not in SUPPORTED_MODELS[provider]:
        raise ValueError(
            f"Unknown model '{model}' for provider '{provider}'. "
            f"Choose from: {SUPPORTED_MODELS[provider]}"
        )


async def _dispatch(
    provider: str,
    model: str,
    api_key: str,
    system_message: str,
    prompt: str,
    max_tokens: int,
) -> str:
    if provider == "openai":
        return await _call_openai(api_key, model, system_message, prompt, max_tokens)
    elif provider == "gemini":
        return await _call_gemini(api_key, model, system_message, prompt, max_tokens)
    elif provider == "claude":
        return await _call_claude(api_key, model, system_message, prompt, max_tokens)
    raise ValueError(f"Unknown provider: {provider}")


def _is_openai_reasoning(model: str) -> bool:
    """Return True for o-series reasoning models (o1, o3, o4-mini, etc.)."""
    return any(model.startswith(p) for p in _OPENAI_REASONING_PREFIXES)


async def _call_openai(
    api_key: str,
    model: str,
    system_message: str,
    prompt: str,
    max_tokens: int,
) -> str:
    import openai
    client = openai.AsyncOpenAI(api_key=api_key)

    reasoning = _is_openai_reasoning(model)

    if reasoning:
        messages = [
            {"role": "developer", "content": system_message},
            {"role": "user", "content": prompt},
        ]
    else:
        messages = [
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt},
        ]

    kwargs: dict = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": max_tokens,
    }
    if not reasoning:
        kwargs["response_format"] = {"type": "json_object"}

    last_exc: Exception = RuntimeError("OpenAI call not attempted")
    for attempt in range(_LLM_MAX_RETRIES + 1):
        try:
            response = await client.chat.completions.create(**kwargs)
            return response.choices[0].message.content.strip()
        except openai.RateLimitError as e:
            last_exc = e
            if attempt < _LLM_MAX_RETRIES:
                delay = _LLM_BASE_DELAY * (2 ** attempt)
                logger.warning("OpenAI rate limited, retry %d/%d in %.1fs", attempt + 1, _LLM_MAX_RETRIES, delay)
                await asyncio.sleep(delay)
                continue
        except openai.APIStatusError as e:
            if e.status_code >= 500 and attempt < _LLM_MAX_RETRIES:
                last_exc = e
                delay = _LLM_BASE_DELAY * (2 ** attempt)
                logger.warning("OpenAI server error %d, retry %d/%d in %.1fs", e.status_code, attempt + 1, _LLM_MAX_RETRIES, delay)
                await asyncio.sleep(delay)
                continue
            # 400, 401, 403 — don't retry
            logger.error("OpenAI call failed (status %d): %s", e.status_code, e)
            raise RuntimeError(f"OpenAI error: {e}") from e
        except Exception as e:
            logger.error("OpenAI call failed: %s", e)
            raise RuntimeError(f"OpenAI error: {e}") from e

    logger.error("OpenAI call failed after retries: %s", last_exc)
    raise RuntimeError(f"OpenAI error after retries: {last_exc}") from last_exc


async def _call_gemini(
    api_key: str,
    model: str,
    system_message: str,
    prompt: str,
    max_tokens: int,
) -> str:
    import google.generativeai as genai

    genai.configure(api_key=api_key)
    gmodel = genai.GenerativeModel(
        model_name=model,
        system_instruction=system_message,
    )

    last_exc: Exception = RuntimeError("Gemini call not attempted")
    for attempt in range(_LLM_MAX_RETRIES + 1):
        try:
            response = await gmodel.generate_content_async(
                prompt,
                generation_config={
                    "temperature": 0.2,
                    "max_output_tokens": max_tokens,
                },
            )
            return response.text.strip()
        except Exception as e:
            last_exc = e
            err_str = str(e).lower()
            # Retry on rate limit or server errors
            is_retryable = "429" in err_str or "500" in err_str or "502" in err_str or "503" in err_str or "rate" in err_str
            if is_retryable and attempt < _LLM_MAX_RETRIES:
                delay = _LLM_BASE_DELAY * (2 ** attempt)
                logger.warning("Gemini transient error, retry %d/%d in %.1fs: %s", attempt + 1, _LLM_MAX_RETRIES, delay, e)
                await asyncio.sleep(delay)
                continue
            logger.error("Gemini call failed: %s", e)
            raise RuntimeError(f"Gemini error: {e}") from e

    logger.error("Gemini call failed after retries: %s", last_exc)
    raise RuntimeError(f"Gemini error after retries: {last_exc}") from last_exc


async def _call_claude(
    api_key: str,
    model: str,
    system_message: str,
    prompt: str,
    max_tokens: int,
) -> str:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key)

    last_exc: Exception = RuntimeError("Claude call not attempted")
    for attempt in range(_LLM_MAX_RETRIES + 1):
        try:
            response = await client.messages.create(
                model=model,
                system=system_message,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
            )
            return response.content[0].text.strip()
        except anthropic.RateLimitError as e:
            last_exc = e
            if attempt < _LLM_MAX_RETRIES:
                delay = _LLM_BASE_DELAY * (2 ** attempt)
                logger.warning("Claude rate limited, retry %d/%d in %.1fs", attempt + 1, _LLM_MAX_RETRIES, delay)
                await asyncio.sleep(delay)
                continue
        except anthropic.APIStatusError as e:
            if e.status_code >= 500 and attempt < _LLM_MAX_RETRIES:
                last_exc = e
                delay = _LLM_BASE_DELAY * (2 ** attempt)
                logger.warning("Claude server error %d, retry %d/%d in %.1fs", e.status_code, attempt + 1, _LLM_MAX_RETRIES, delay)
                await asyncio.sleep(delay)
                continue
            logger.error("Claude call failed (status %d): %s", e.status_code, e)
            raise RuntimeError(f"Claude error: {e}") from e
        except Exception as e:
            logger.error("Claude call failed: %s", e)
            raise RuntimeError(f"Claude error: {e}") from e

    logger.error("Claude call failed after retries: %s", last_exc)
    raise RuntimeError(f"Claude error after retries: {last_exc}") from last_exc

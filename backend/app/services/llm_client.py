"""
Unified async LLM client supporting OpenAI, Google Gemini, and Anthropic Claude.
No third-party wrappers — direct SDK calls only.

Supports:
- Updated model lists (March 2026): Claude 4.x, GPT-4.1, Gemini 2.5
- Fallback chain: if primary provider fails, tries next configured provider
- Per-call retry with exponential backoff for transient errors (typed exceptions)
- OpenAI o-series: developer role, max_completion_tokens, no response_format
- OpenAI standard: response_format=json_object for reliable JSON output
- Gemini: native async (no run_in_executor)
- Per-provider token limits
- Usage accounting + per-day USD spend cap (LLM_DAILY_USD_CAP)
"""
import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Module-level SDK references — imported eagerly when available so tests can
# `patch("app.services.llm_client.openai")` etc. The provider helpers also
# fall back to local imports if these are None (e.g. in minimal test envs).
try:  # pragma: no cover — env-dependent
    import openai  # type: ignore
except Exception:
    openai = None  # type: ignore

try:  # pragma: no cover
    # Prefer the new `google.genai` SDK. The legacy `google.generativeai`
    # package is officially deprecated and no longer receives updates.
    from google import genai as _new_genai  # type: ignore
    genai = _new_genai
    _GENAI_FLAVOR = "new"
except Exception:
    try:
        import google.generativeai as genai  # type: ignore
        _GENAI_FLAVOR = "legacy"
    except Exception:
        genai = None  # type: ignore
        _GENAI_FLAVOR = "none"

try:  # pragma: no cover
    import anthropic  # type: ignore
except Exception:
    anthropic = None  # type: ignore


# Snapshot the SDK exception classes at import time. Tests patch the module
# names (openai/genai/anthropic) with MagicMock, which would otherwise turn
# `except openai.RateLimitError` into a TypeError ("catching classes that do
# not inherit from BaseException is not allowed"). Snapshotting the classes
# below makes the except-clauses immune to those patches.
class _Unraisable(Exception):
    """Sentinel — never raised, never matched. Used when an SDK is missing."""


_OAI_RATE = getattr(openai, "RateLimitError", _Unraisable) if openai else _Unraisable
_OAI_STATUS = getattr(openai, "APIStatusError", _Unraisable) if openai else _Unraisable
_OAI_CONN = getattr(openai, "APIConnectionError", _Unraisable) if openai else _Unraisable
_OAI_TIMEOUT = getattr(openai, "APITimeoutError", _Unraisable) if openai else _Unraisable

_ANT_RATE = getattr(anthropic, "RateLimitError", _Unraisable) if anthropic else _Unraisable
_ANT_STATUS = getattr(anthropic, "APIStatusError", _Unraisable) if anthropic else _Unraisable
_ANT_CONN = getattr(anthropic, "APIConnectionError", _Unraisable) if anthropic else _Unraisable
_ANT_TIMEOUT = getattr(anthropic, "APITimeoutError", _Unraisable) if anthropic else _Unraisable

# ── Retry constants for individual provider calls ─────────────
_LLM_MAX_RETRIES = 2
_LLM_BASE_DELAY = 1.0  # seconds

# Default USD→INR conversion. Override via env. Must be > 0.
_DEFAULT_USD_INR = 83.0


def _usd_inr() -> float:
    """Read USD_INR from env; clamp to a sane band so a typo can't 100x cost."""
    raw = os.getenv("USD_INR", "")
    try:
        v = float(raw) if raw else _DEFAULT_USD_INR
    except ValueError:
        v = _DEFAULT_USD_INR
    if v <= 0 or v > 1000:
        return _DEFAULT_USD_INR
    return v


# ─────────────────────────────────────────────
# Spend cap
# ─────────────────────────────────────────────

class LLMSpendCapExceeded(RuntimeError):
    """Raised when a call would exceed the configured daily USD spend cap."""


async def _get_daily_cap_usd() -> float:
    """Return the daily cap in USD. 0 disables. Settings table wins over env."""
    # 1) settings table
    try:
        from app.database import connect
        async with connect() as db:
            cursor = await db.execute(
                "SELECT value FROM settings WHERE key = ?",
                ("LLM_DAILY_USD_CAP",),
            )
            row = await cursor.fetchone()
            if row and row[0] is not None:
                try:
                    v = float(row[0])
                    if v >= 0:
                        return v
                except (TypeError, ValueError):
                    pass
    except Exception as exc:
        logger.debug("daily cap settings read failed (non-fatal): %s", exc)
    # 2) env fallback
    raw = os.getenv("LLM_DAILY_USD_CAP", "")
    try:
        v = float(raw) if raw else 0.0
    except ValueError:
        v = 0.0
    return max(v, 0.0)


async def _enforce_daily_cap() -> None:
    """Raise LLMSpendCapExceeded if today's spend has already met/exceeded cap."""
    cap = await _get_daily_cap_usd()
    if cap <= 0:
        return  # disabled
    from app.database import get_today_llm_spend_usd
    spent = await get_today_llm_spend_usd()
    if spent >= cap:
        raise LLMSpendCapExceeded(
            f"Daily LLM spend cap reached: ${spent:.4f} >= ${cap:.4f}"
        )


# ─────────────────────────────────────────────
# Pricing — USD per 1k tokens
# Conservative public pricing snapshots; pricing changes are forward-only,
# historical rows keep the cost computed at write-time.
# ─────────────────────────────────────────────

_PRICING: dict[str, dict[str, tuple[float, float]]] = {
    # provider → model → (input_per_1k, output_per_1k)
    "openai": {
        "gpt-5":         (0.005,  0.015),
        "gpt-5-mini":    (0.0008, 0.0024),
        "gpt-5-nano":    (0.0002, 0.0006),
        "gpt-4.1":       (0.005,  0.015),
        "gpt-4.1-mini":  (0.0008, 0.0024),
        "o4-mini":       (0.0011, 0.0044),
        "o3":            (0.010,  0.040),
    },
    "gemini": {
        "gemini-3.1-pro":    (0.00125, 0.005),
        "gemini-3.1-flash":  (0.000075, 0.0003),
        "gemini-3-flash":    (0.000075, 0.0003),
        "gemini-2.5-pro":    (0.00125, 0.005),
    },
    "claude": {
        "claude-opus-4-7":            (0.015,  0.075),
        "claude-sonnet-4-5":          (0.003,  0.015),
        "claude-haiku-4-5-20251001":  (0.00025, 0.00125),
        "claude-sonnet-4-6":          (0.003,  0.015),
    },
}


def _compute_cost_usd(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    rates = _PRICING.get(provider, {}).get(model)
    if not rates:
        return 0.0
    in_rate, out_rate = rates
    return (prompt_tokens / 1000.0) * in_rate + (completion_tokens / 1000.0) * out_rate


# ─────────────────────────────────────────────
# Model registry
# ─────────────────────────────────────────────

SUPPORTED_MODELS: dict[str, list[str]] = {
    "openai": [
        "gpt-5",             # GPT-5 flagship
        "gpt-5-mini",        # fast + cheap GPT-5 tier
        "gpt-5-nano",        # cheapest GPT-5
        "gpt-4.1",           # GPT-4.1 — kept for fallback
        "gpt-4.1-mini",      # GPT-4.1 mini fallback
        "o4-mini",           # reasoning, budget
        "o3",                # reasoning legacy
    ],
    "gemini": [
        "gemini-3.1-pro",      # Gemini 3.1 Pro flagship
        "gemini-3.1-flash",    # Gemini 3.1 Flash — fast + cheap
        "gemini-3-flash",      # Gemini 3 Flash fallback
        "gemini-2.5-pro",      # legacy, kept for compat
    ],
    "claude": [
        "claude-opus-4-7",             # Opus 4.7 — most capable
        "claude-sonnet-4-5",           # Sonnet 4.5 — balanced default
        "claude-haiku-4-5-20251001",   # Haiku 4.5 — fast + cheap
        "claude-sonnet-4-6",           # legacy, kept for compat
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
    *,
    route: Optional[str] = None,
    symbol: Optional[str] = None,
) -> str:
    """
    Call the specified LLM provider and return the raw text response.

    If the primary call fails and fallback_chain is provided, each entry
    (provider, model, api_key) is tried in order until one succeeds.

    `route` and `symbol` are recorded with usage for cost analysis.

    Raises:
        ValueError:            Unknown provider/model or missing key (not retried).
        LLMSpendCapExceeded:   Daily USD spend cap reached BEFORE attempting any call.
        RuntimeError:          All API calls failed after exhausting fallback chain.
    """
    if not api_key:
        raise ValueError("No API key configured. Please set your API key in Settings.")

    provider = provider.lower().strip()
    _validate_provider_model(provider, model)

    # Check cap once up-front. Cheaper than checking per-attempt and the
    # cap is a soft guard, not a hard token-level meter.
    await _enforce_daily_cap()

    candidates: list[tuple[str, str, str]] = [(provider, model, api_key)]
    if fallback_chain:
        candidates.extend(fallback_chain)

    last_exc: Exception = RuntimeError("No providers attempted.")
    for prov, mod, key in candidates:
        if not key:
            continue
        try:
            tokens = max_tokens or _DEFAULT_MAX_TOKENS.get(prov, 2048)
            result = await _dispatch(prov, mod, key, system_message, prompt, tokens)
            # Back-compat: _dispatch historically returned `str`; we now return
            # (text, usage). Accept either so existing tests that mock _dispatch
            # with a plain string keep working.
            if isinstance(result, tuple) and len(result) == 2:
                text, usage = result
            else:
                text, usage = result, {}
            await _record_success(prov, mod, usage or {}, route=route, symbol=symbol)
            return text
        except ValueError:
            # Config errors (bad model/key format) — don't retry
            raise
        except Exception as exc:
            last_exc = exc
            await _record_failure(prov, mod, route=route, symbol=symbol)
            logger.warning(
                "LLM call failed for %s/%s, trying next in chain: %s",
                prov, mod, exc,
            )

    raise RuntimeError(
        f"All LLM providers exhausted. Last error: {last_exc}"
    ) from last_exc


# ─────────────────────────────────────────────
# Usage recording helpers
# ─────────────────────────────────────────────

async def _record_success(
    provider: str,
    model: str,
    usage: dict,
    *,
    route: Optional[str],
    symbol: Optional[str],
) -> None:
    pt = int(usage.get("prompt_tokens") or 0)
    ct = int(usage.get("completion_tokens") or 0)
    cost_usd = _compute_cost_usd(provider, model, pt, ct)
    cost_inr = cost_usd * _usd_inr()
    try:
        from app.database import record_llm_usage
        await record_llm_usage(
            provider=provider,
            model=model,
            prompt_tokens=pt,
            completion_tokens=ct,
            cost_usd=cost_usd,
            cost_inr=cost_inr,
            request_id=usage.get("request_id"),
            route=route,
            symbol=symbol,
            success=True,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("usage record (success) failed: %s", exc)


async def _record_failure(
    provider: str,
    model: str,
    *,
    route: Optional[str],
    symbol: Optional[str],
) -> None:
    try:
        from app.database import record_llm_usage
        await record_llm_usage(
            provider=provider,
            model=model,
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=0.0,
            cost_inr=0.0,
            route=route,
            symbol=symbol,
            success=False,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("usage record (failure) failed: %s", exc)


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
) -> tuple[str, dict]:
    """Return (text, usage_dict). usage_dict keys: prompt_tokens, completion_tokens, request_id."""
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


def _http_status_of(exc: Exception) -> Optional[int]:
    """Best-effort: pull a status code off an SDK exception. Used as a
    last-resort retry signal when the SDK's typed hierarchy doesn't match."""
    for attr in ("status_code", "code", "http_status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(exc, "response", None)
    if resp is not None:
        v = getattr(resp, "status_code", None)
        if isinstance(v, int):
            return v
    return None


async def _call_openai(
    api_key: str,
    model: str,
    system_message: str,
    prompt: str,
    max_tokens: int,
) -> tuple[str, dict]:
    if openai is None:  # pragma: no cover
        raise RuntimeError("openai SDK not installed")
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
            text = response.choices[0].message.content.strip()
            usage_obj = getattr(response, "usage", None)
            usage = {
                "prompt_tokens": getattr(usage_obj, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(usage_obj, "completion_tokens", 0) or 0,
                "request_id": getattr(response, "id", None),
            }
            return text, usage
        except _OAI_RATE as e:
            last_exc = e
            if attempt < _LLM_MAX_RETRIES:
                delay = _LLM_BASE_DELAY * (2 ** attempt)
                logger.warning("OpenAI rate limited, retry %d/%d in %.1fs", attempt + 1, _LLM_MAX_RETRIES, delay)
                await asyncio.sleep(delay)
                continue
        except _OAI_STATUS as e:
            status = e.status_code if isinstance(getattr(e, "status_code", None), int) else _http_status_of(e)
            if status is not None and status >= 500 and attempt < _LLM_MAX_RETRIES:
                last_exc = e
                delay = _LLM_BASE_DELAY * (2 ** attempt)
                logger.warning("OpenAI server error %d, retry %d/%d in %.1fs", status, attempt + 1, _LLM_MAX_RETRIES, delay)
                await asyncio.sleep(delay)
                continue
            # 400, 401, 403 — don't retry
            logger.error("OpenAI call failed (status %s): %s", status, e)
            raise RuntimeError(f"OpenAI error: {e}") from e
        except (_OAI_CONN, _OAI_TIMEOUT) as e:
            last_exc = e
            if attempt < _LLM_MAX_RETRIES:
                delay = _LLM_BASE_DELAY * (2 ** attempt)
                logger.warning("OpenAI transport error, retry %d/%d in %.1fs: %s", attempt + 1, _LLM_MAX_RETRIES, delay, e)
                await asyncio.sleep(delay)
                continue
        except Exception as e:
            logger.error("OpenAI call failed: %s", e)
            raise RuntimeError(f"OpenAI error: {e}") from e

    logger.error("OpenAI call failed after retries: %s", last_exc)
    raise RuntimeError(f"OpenAI error after retries: {last_exc}") from last_exc


def _gemini_retryable_exc_types():
    """Lazy: return a tuple of google-api-core exception classes worth retrying.
    Returns () if google.api_core is not installed (test env)."""
    try:
        from google.api_core import exceptions as gax  # type: ignore
        return (
            gax.ResourceExhausted,    # 429
            gax.ServiceUnavailable,   # 503
            gax.InternalServerError,  # 500
            gax.DeadlineExceeded,     # 504-ish
            gax.Aborted,
        )
    except Exception:
        return tuple()


async def _call_gemini(
    api_key: str,
    model: str,
    system_message: str,
    prompt: str,
    max_tokens: int,
) -> tuple[str, dict]:
    if genai is None:  # pragma: no cover
        raise RuntimeError("google.genai SDK not installed")

    # Branch by SDK flavor — `google.genai` is the new SDK with
    # `Client(...).aio.models.generate_content(...)`. The legacy
    # `google.generativeai` exposes `GenerativeModel(...)` and is deprecated.
    use_new = _GENAI_FLAVOR == "new"
    if use_new:
        client = genai.Client(api_key=api_key)
        from google.genai import types as genai_types  # type: ignore
        config = genai_types.GenerateContentConfig(
            system_instruction=system_message,
            temperature=0.2,
            max_output_tokens=max_tokens,
        )
    else:
        genai.configure(api_key=api_key)  # legacy
        gmodel = genai.GenerativeModel(
            model_name=model,
            system_instruction=system_message,
        )

    retryable_types = _gemini_retryable_exc_types()

    last_exc: Exception = RuntimeError("Gemini call not attempted")
    for attempt in range(_LLM_MAX_RETRIES + 1):
        try:
            if use_new:
                response = await client.aio.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=config,
                )
            else:
                response = await gmodel.generate_content_async(
                    prompt,
                    generation_config={
                        "temperature": 0.2,
                        "max_output_tokens": max_tokens,
                    },
                )
            text = (response.text or "").strip()
            # Gemini exposes usage_metadata with prompt_token_count / candidates_token_count
            um = getattr(response, "usage_metadata", None)
            usage = {
                "prompt_tokens": getattr(um, "prompt_token_count", 0) or 0,
                "completion_tokens": getattr(um, "candidates_token_count", 0) or 0,
                "request_id": None,
            }
            return text, usage
        except Exception as e:
            last_exc = e
            # Prefer typed-exception detection over string matching.
            is_retryable = isinstance(e, retryable_types) if retryable_types else False
            # Fallback: status code on response/exception (some SDK paths wrap
            # the upstream error). Never fall back to string matching.
            if not is_retryable:
                status = _http_status_of(e)
                if status in (429, 500, 502, 503, 504):
                    is_retryable = True
            if is_retryable and attempt < _LLM_MAX_RETRIES:
                delay = _LLM_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "Gemini transient error (%s), retry %d/%d in %.1fs",
                    type(e).__name__, attempt + 1, _LLM_MAX_RETRIES, delay,
                )
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
) -> tuple[str, dict]:
    if anthropic is None:  # pragma: no cover
        raise RuntimeError("anthropic SDK not installed")
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
            text = response.content[0].text.strip()
            usage_obj = getattr(response, "usage", None)
            usage = {
                "prompt_tokens": getattr(usage_obj, "input_tokens", 0) or 0,
                "completion_tokens": getattr(usage_obj, "output_tokens", 0) or 0,
                "request_id": getattr(response, "id", None),
            }
            return text, usage
        except _ANT_RATE as e:
            last_exc = e
            if attempt < _LLM_MAX_RETRIES:
                delay = _LLM_BASE_DELAY * (2 ** attempt)
                logger.warning("Claude rate limited, retry %d/%d in %.1fs", attempt + 1, _LLM_MAX_RETRIES, delay)
                await asyncio.sleep(delay)
                continue
        except _ANT_STATUS as e:
            status = e.status_code if isinstance(getattr(e, "status_code", None), int) else _http_status_of(e)
            if status is not None and status >= 500 and attempt < _LLM_MAX_RETRIES:
                last_exc = e
                delay = _LLM_BASE_DELAY * (2 ** attempt)
                logger.warning("Claude server error %d, retry %d/%d in %.1fs", status, attempt + 1, _LLM_MAX_RETRIES, delay)
                await asyncio.sleep(delay)
                continue
            logger.error("Claude call failed (status %s): %s", status, e)
            raise RuntimeError(f"Claude error: {e}") from e
        except (_ANT_CONN, _ANT_TIMEOUT) as e:
            last_exc = e
            if attempt < _LLM_MAX_RETRIES:
                delay = _LLM_BASE_DELAY * (2 ** attempt)
                logger.warning("Claude transport error, retry %d/%d in %.1fs: %s", attempt + 1, _LLM_MAX_RETRIES, delay, e)
                await asyncio.sleep(delay)
                continue
        except Exception as e:
            logger.error("Claude call failed: %s", e)
            raise RuntimeError(f"Claude error: {e}") from e

    logger.error("Claude call failed after retries: %s", last_exc)
    raise RuntimeError(f"Claude error after retries: {last_exc}") from last_exc

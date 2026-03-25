"""
Unified async LLM client supporting OpenAI, Google Gemini, and Anthropic Claude.
No third-party wrappers — direct SDK calls only.
Forked from FinSight/backend/llm_client.py (image support removed).
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

SUPPORTED_MODELS = {
    "openai": ["gpt-4o-mini", "gpt-4o", "gpt-4-turbo", "o1", "o3-mini"],
    "gemini": ["gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash"],
    "claude": ["claude-3-5-sonnet-20241022", "claude-3-opus-20240229", "claude-3-5-haiku-20241022"],
}


async def call_llm(
    provider: str,
    model: str,
    api_key: str,
    prompt: str,
    system_message: str = "You are an expert Indian stock market analyst. Respond with valid JSON only.",
) -> str:
    """
    Call the specified LLM provider and return the raw text response.

    Args:
        provider: "openai" | "gemini" | "claude"
        model:    Model name (must be in SUPPORTED_MODELS[provider])
        api_key:  API key for the provider
        prompt:   User message / instruction
        system_message: System-level instruction

    Returns:
        Raw string response from the model.

    Raises:
        ValueError: Unknown provider / model or missing key.
        RuntimeError: API call failure.
    """
    if not api_key:
        raise ValueError("No API key configured. Please set your API key in Settings.")

    provider = provider.lower().strip()
    if provider not in SUPPORTED_MODELS:
        raise ValueError(f"Unknown provider '{provider}'. Choose from: {list(SUPPORTED_MODELS)}")
    if model not in SUPPORTED_MODELS[provider]:
        raise ValueError(f"Unknown model '{model}' for provider '{provider}'. Choose from: {SUPPORTED_MODELS[provider]}")

    if provider == "openai":
        return await _call_openai(api_key, model, system_message, prompt)
    elif provider == "gemini":
        return await _call_gemini(api_key, model, system_message, prompt)
    elif provider == "claude":
        return await _call_claude(api_key, model, system_message, prompt)


async def _call_openai(api_key: str, model: str, system_message: str, prompt: str) -> str:
    try:
        import openai
        client = openai.AsyncOpenAI(api_key=api_key)
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt},
            ],
            max_completion_tokens=2048,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OpenAI call failed: {e}")
        raise RuntimeError(f"OpenAI error: {e}")


async def _call_gemini(api_key: str, model: str, system_message: str, prompt: str) -> str:
    try:
        import google.generativeai as genai
        import asyncio

        genai.configure(api_key=api_key)
        gmodel = genai.GenerativeModel(
            model_name=model,
            system_instruction=system_message,
        )
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: gmodel.generate_content(
                prompt,
                generation_config={"temperature": 0.2, "max_output_tokens": 2048},
            ),
        )
        return response.text.strip()
    except Exception as e:
        logger.error(f"Gemini call failed: {e}")
        raise RuntimeError(f"Gemini error: {e}")


async def _call_claude(api_key: str, model: str, system_message: str, prompt: str) -> str:
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)
        response = await client.messages.create(
            model=model,
            system=system_message,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048,
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude call failed: {e}")
        raise RuntimeError(f"Claude error: {e}")

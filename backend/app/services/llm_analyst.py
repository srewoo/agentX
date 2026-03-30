from __future__ import annotations
"""
LLM prompt building and analysis orchestration.
Bridges signal_engine (deterministic) and llm_client (non-deterministic).

Architecture rule: Signal engine NEVER calls LLM. Only this module does.

Security: All external-sourced values are sanitized before prompt interpolation
to prevent prompt injection attacks.
"""
import logging
import re
from typing import Any, Optional

from app.utils import parse_llm_json, safe_float
from app.services.llm_client import call_llm, SUPPORTED_MODELS
from app.services.fundamentals import get_fundamentals

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Per-provider token limits for prompt budget
# ─────────────────────────────────────────────
_PROVIDER_MAX_OUTPUT_TOKENS: dict[str, int] = {
    "openai": 4096,
    "gemini": 2048,
    "claude": 4096,
}

# Valid stances the LLM is allowed to return
_VALID_STANCES = {"BUY", "SELL", "HOLD", "CAUTIOUS_BUY", "CAUTIOUS_SELL"}

# Regex to strip all control characters (newline, tab, carriage return, etc.)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


# ─────────────────────────────────────────────
# Input sanitization
# ─────────────────────────────────────────────

def _sanitize_for_prompt(value: Any, max_len: int = 200) -> str:
    """
    Sanitize a value before interpolating it into an LLM prompt.

    - Converts to string
    - Removes all control characters (newlines, tabs, etc.) that could inject
      new prompt sections or escape the intended context
    - Collapses excess whitespace
    - Truncates to max_len

    Returns "N/A" for None/empty values.
    """
    if value is None:
        return "N/A"
    s = str(value).strip()
    if not s:
        return "N/A"
    # Remove control chars (most dangerous: \n allows new prompt injection lines)
    s = _CONTROL_CHARS_RE.sub(" ", s)
    # Collapse multiple spaces
    s = re.sub(r" {2,}", " ", s).strip()
    return s[:max_len]


def _pct_display(val: Any) -> str:
    """Convert a decimal ratio (e.g. 0.15) to a percentage string (e.g. '15.0'), or 'N/A'."""
    f = safe_float(val)
    if f is None:
        return "N/A"
    return str(round(f * 100, 1))


# ─────────────────────────────────────────────
# Output validation
# ─────────────────────────────────────────────

def _validate_analysis_output(result: dict, fallback: dict) -> dict:
    """
    Validate and sanitize the LLM's analysis JSON response.

    Returns the fallback if the stance is invalid or result is not a dict.
    Clamps numeric fields and sanitizes string fields.
    """
    if not isinstance(result, dict):
        return fallback

    # Validate stance
    stance = str(result.get("stance", "")).upper().strip()
    if stance not in _VALID_STANCES:
        logger.warning("LLM returned invalid stance '%s', using fallback", stance)
        return fallback

    # Clamp confidence to [0, 100]
    try:
        confidence = max(0, min(100, int(result.get("confidence", 50))))
    except (TypeError, ValueError):
        confidence = 50

    # Coerce list fields
    key_reasons = result.get("key_reasons", [])
    if not isinstance(key_reasons, list):
        key_reasons = [str(key_reasons)] if key_reasons else fallback["key_reasons"]
    key_reasons = [_sanitize_for_prompt(r, max_len=300) for r in key_reasons[:5]]

    risks = result.get("risks", [])
    if not isinstance(risks, list):
        risks = [str(risks)] if risks else fallback["risks"]
    risks = [_sanitize_for_prompt(r, max_len=300) for r in risks[:4]]

    # Sanitize string fields
    summary = _sanitize_for_prompt(result.get("summary", ""), max_len=600)
    technical_outlook = _sanitize_for_prompt(result.get("technical_outlook", ""), max_len=400)
    sentiment = _sanitize_for_prompt(result.get("sentiment", "Neutral"), max_len=20)
    support_zone = _sanitize_for_prompt(result.get("support_zone", ""), max_len=50)
    resistance_zone = _sanitize_for_prompt(result.get("resistance_zone", ""), max_len=50)

    # Validate sentiment value
    if sentiment not in ("Bullish", "Bearish", "Neutral"):
        sentiment = "Neutral"

    return {
        "stance": stance,
        "confidence": confidence,
        "summary": summary or fallback["summary"],
        "key_reasons": key_reasons or fallback["key_reasons"],
        "risks": risks or fallback["risks"],
        "technical_outlook": technical_outlook,
        "sentiment": sentiment,
        "support_zone": support_zone,
        "resistance_zone": resistance_zone,
    }


# ─────────────────────────────────────────────
# Fallback chain construction
# ─────────────────────────────────────────────

def _get_api_key(settings: dict, provider: str) -> str:
    """Resolve API key: generic key first, then provider-specific."""
    generic = settings.get("llm_api_key", "").strip()
    if generic:
        return generic
    return settings.get(f"{provider}_api_key", "").strip()


def _build_fallback_chain(
    settings: dict,
    primary_provider: str,
) -> list[tuple[str, str, str]]:
    """
    Build a fallback chain from other configured providers.

    Returns a list of (provider, model, api_key) tuples — cheapest/fastest
    models used for fallback to minimise cost.
    """
    # Default fallback models per provider (fast + cheap)
    _fallback_defaults: dict[str, tuple[str, str]] = {
        "openai": ("openai", "gpt-4.1-mini"),
        "gemini": ("gemini", "gemini-2.0-flash"),
        "claude":  ("claude",  "claude-haiku-4-5-20251001"),
    }

    chain: list[tuple[str, str, str]] = []
    for prov, (p, default_model) in _fallback_defaults.items():
        if prov == primary_provider:
            continue
        key = settings.get(f"{prov}_api_key", "").strip()
        if key:
            chain.append((p, default_model, key))
    return chain


# ─────────────────────────────────────────────
# Signal enrichment (called by orchestrator — max 1/cycle)
# ─────────────────────────────────────────────

async def enrich_signal(signal: dict[str, Any], technicals: dict, settings: dict) -> str:
    """
    Generate a 2-3 sentence LLM narrative for a detected signal.
    Returns the summary string, or empty string on failure.
    """
    provider = settings.get("llm_provider", "gemini")
    model = settings.get("llm_model", "gemini-2.0-flash")
    api_key = _get_api_key(settings, provider)

    if not api_key:
        return ""

    fallback_chain = _build_fallback_chain(settings, provider)

    # Sanitize all external values before prompt interpolation
    symbol = _sanitize_for_prompt(signal["symbol"], max_len=20)
    signal_type = _sanitize_for_prompt(signal["signal_type"], max_len=50)
    direction = _sanitize_for_prompt(signal["direction"], max_len=20)
    reason = _sanitize_for_prompt(signal["reason"], max_len=300)
    current_price = safe_float(signal.get("current_price")) or "N/A"
    rsi = safe_float(technicals.get("rsi")) or "N/A"
    adx = safe_float(technicals.get("adx")) or "N/A"
    macd_sig = _sanitize_for_prompt(
        technicals.get("macd", {}).get("signal", "N/A"), max_len=20
    )
    regime = _sanitize_for_prompt(
        (technicals.get("market_regime") or {}).get("regime", "N/A"), max_len=30
    )

    prompt = f"""A technical signal was detected for {symbol}. Provide a brief 2-3 sentence analysis.

Signal: {signal_type.replace('_', ' ').title()} ({direction})
Trigger: {reason}
Current Price: {current_price}

Technical Context:
- RSI: {rsi}
- ADX (trend strength): {adx}
- MACD: {macd_sig}
- Market Regime: {regime}

Instructions:
- Explain WHY this signal matters in simple terms
- Mention 1 supporting factor and 1 risk factor
- Do NOT predict prices or give buy/sell recommendations
- Keep it factual and grounded in the data above

Return ONLY valid JSON:
{{"summary": "2-3 sentence explanation", "key_factor": "one supporting factor", "risk": "one risk factor"}}"""

    try:
        response = await call_llm(
            provider=provider,
            model=model,
            api_key=api_key,
            prompt=prompt,
            system_message="You are a concise Indian stock market analyst. Explain signals clearly without making predictions. Return ONLY valid JSON.",
            max_tokens=_PROVIDER_MAX_OUTPUT_TOKENS.get(provider, 2048),
            fallback_chain=fallback_chain,
        )
        result = parse_llm_json(response, {"summary": "", "key_factor": "", "risk": ""})
        summary = _sanitize_for_prompt(result.get("summary", ""), max_len=500)
        key = _sanitize_for_prompt(result.get("key_factor", ""), max_len=200)
        risk = _sanitize_for_prompt(result.get("risk", ""), max_len=200)
        parts = [
            p for p in [
                summary,
                key and f"Key factor: {key}",
                risk and f"Risk: {risk}",
            ] if p and p != "N/A"
        ]
        return " ".join(parts)
    except Exception as e:
        logger.warning("Failed to enrich signal for %s: %s", symbol, e)
        return ""


# ─────────────────────────────────────────────
# Full AI analysis (user-triggered, on-demand)
# ─────────────────────────────────────────────

async def run_analysis(
    symbol: str,
    timeframe: str,
    technicals: dict,
    sr: dict,
    fib: dict,
    poc: Optional[float],
    stock_info: dict,
    settings: dict,
    fundamentals: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Full AI analysis for a stock (user-triggered, on-demand).
    Returns structured analysis dict.
    """
    provider = settings.get("llm_provider", "gemini")
    model = settings.get("llm_model", "gemini-2.0-flash")
    api_key = _get_api_key(settings, provider)

    # Use provided fundamentals or fetch them (best-effort)
    if fundamentals is None:
        try:
            fundamentals = await get_fundamentals(symbol)
        except Exception as e:
            logger.warning("Fundamentals fetch failed for %s: %s", symbol, e)
            fundamentals = {}

    # Sanitize all external values
    sym = _sanitize_for_prompt(symbol, max_len=20)
    stock_name = _sanitize_for_prompt(stock_info.get("name", symbol), max_len=100)
    sector = _sanitize_for_prompt(stock_info.get("sector", "N/A"), max_len=60)
    pe_ratio = safe_float(stock_info.get("pe_ratio")) or "N/A"
    market_cap = _sanitize_for_prompt(str(stock_info.get("market_cap", "N/A")), max_len=30)
    current_price = safe_float(technicals.get("current_price")) or "N/A"

    timeframe_map = {
        "intraday": "intraday (today's session)",
        "swing": "swing trade (1–2 weeks)",
        "long": "long-term investment (3–12 months)",
        "short": "short-term (1 week to 1 month)",
    }
    timeframe_desc = timeframe_map.get(timeframe, "short-term (1 week to 1 month)")

    ma = technicals.get("moving_averages", {})
    macd = technicals.get("macd", {})
    bb = technicals.get("bollinger_bands", {})
    resistance = sr.get("resistance", {})
    support = sr.get("support", {})
    fib_levels = fib.get("levels", {})

    fallback: dict[str, Any] = {
        "stance": "HOLD",
        "confidence": 50,
        "summary": "Analysis unavailable. Please check your LLM API key.",
        "key_reasons": ["Insufficient data"],
        "risks": ["Market volatility"],
        "technical_outlook": "Mixed signals",
        "sentiment": "Neutral",
        "support_zone": str(support.get("s1", "N/A")),
        "resistance_zone": str(resistance.get("r1", "N/A")),
    }

    if not api_key:
        return fallback

    fallback_chain = _build_fallback_chain(settings, provider)

    prompt = f"""You are a senior Indian stock market analyst AI. Analyze this stock and provide insights for {timeframe_desc}.

STOCK: {stock_name} ({sym})
Sector: {sector} | P/E: {pe_ratio} | Market Cap: {market_cap}
Current Price: {current_price}

TECHNICAL INDICATORS:
- RSI(14): {technicals.get('rsi')} ({technicals.get('rsi_signal')})
- ADX: {technicals.get('adx')} (trend strength)
- MACD: Line={macd.get('macd_line')}, Signal={macd.get('signal_line')}, Histogram={macd.get('histogram')} → {macd.get('signal')}
- SMA20={ma.get('sma20')}, SMA50={ma.get('sma50')}, SMA200={ma.get('sma200')}
- Bollinger: {bb.get('signal')} (Upper={bb.get('upper')}, Lower={bb.get('lower')})

SUPPORT / RESISTANCE:
- Pivot: {sr.get('pivot')}, R1={resistance.get('r1')}, R2={resistance.get('r2')}, S1={support.get('s1')}, S2={support.get('s2')}
- Fibonacci 38.2%={fib_levels.get('level_38_2')}, 50%={fib_levels.get('level_50_0')}, 61.8%={fib_levels.get('level_61_8')}
- Volume POC: {poc}

FUNDAMENTALS:
- P/E: {_sanitize_for_prompt(safe_float((fundamentals.get("valuation") or {}).get("pe")))} (Forward P/E: {_sanitize_for_prompt(safe_float((fundamentals.get("valuation") or {}).get("forward_pe")))})
- ROE: {_sanitize_for_prompt(_pct_display((fundamentals.get("profitability") or {}).get("roe")))}% | ROA: {_sanitize_for_prompt(_pct_display((fundamentals.get("profitability") or {}).get("roa")))}%
- Debt/Equity: {_sanitize_for_prompt(safe_float((fundamentals.get("financial_health") or {}).get("debt_to_equity")))} | Current Ratio: {_sanitize_for_prompt(safe_float((fundamentals.get("financial_health") or {}).get("current_ratio")))}
- Revenue Growth: {_sanitize_for_prompt(_pct_display((fundamentals.get("growth") or {}).get("revenue_growth")))}% | Earnings Growth: {_sanitize_for_prompt(_pct_display((fundamentals.get("growth") or {}).get("earnings_growth")))}%
- Dividend Yield: {_sanitize_for_prompt(_pct_display((fundamentals.get("dividends") or {}).get("yield")))}%
- Fundamental Score: {fundamentals.get("fundamental_score", "N/A")}/10 ({_sanitize_for_prompt(fundamentals.get("fundamental_signal", "N/A"))})

Analysis timeframe: {timeframe_desc}

Instructions:
- Summarize the technical picture objectively
- Identify 2-3 key reasons supporting your view
- Identify 2 key risks
- Give a BUY/SELL/HOLD stance for the specified timeframe
- BUY = setup looks constructive, risk/reward favors upside; SELL = breakdown or deteriorating; HOLD = mixed/unclear
- Do NOT predict exact prices

Return ONLY valid JSON:
{{"stance": "BUY|SELL|HOLD", "confidence": 1-100, "summary": "2-3 sentences", "key_reasons": ["r1","r2","r3"], "risks": ["risk1","risk2"], "technical_outlook": "1-2 sentences", "sentiment": "Bullish|Bearish|Neutral", "support_zone": "price range", "resistance_zone": "price range"}}"""

    try:
        response = await call_llm(
            provider=provider,
            model=model,
            api_key=api_key,
            prompt=prompt,
            system_message="You are an expert Indian stock market analyst. Analyze objectively. Return ONLY valid JSON.",
            max_tokens=_PROVIDER_MAX_OUTPUT_TOKENS.get(provider, 2048),
            fallback_chain=fallback_chain,
        )
        raw = parse_llm_json(response, fallback)
        return _validate_analysis_output(raw, fallback)
    except Exception as e:
        logger.error("AI analysis failed for %s: %s", sym, e)
        return fallback

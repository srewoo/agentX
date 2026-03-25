from __future__ import annotations
"""
LLM prompt building and analysis orchestration.
Bridges signal_engine (deterministic) and llm_client (non-deterministic).
Architecture rule: Signal engine NEVER calls LLM. Only this module does.
"""
import logging
from typing import Any, Optional

from app.utils import parse_llm_json, safe_float
from app.services.llm_client import call_llm

logger = logging.getLogger(__name__)


def _get_api_key(settings: dict, provider: str) -> str:
    """Resolve API key: settings-level key first, then provider-specific."""
    generic = settings.get("llm_api_key", "").strip()
    if generic:
        return generic
    return settings.get(f"{provider}_api_key", "").strip()


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

    symbol = signal["symbol"]
    signal_type = signal["signal_type"]
    direction = signal["direction"]
    reason = signal["reason"]
    current_price = signal.get("current_price", "N/A")
    rsi = technicals.get("rsi", "N/A")
    adx = technicals.get("adx", "N/A")
    macd_sig = technicals.get("macd", {}).get("signal", "N/A")
    regime = technicals.get("market_regime", {}).get("regime", "Unknown") if "market_regime" in technicals else "N/A"

    prompt = f"""A technical signal was detected for {symbol}. Provide a brief 2-3 sentence analysis.

Signal: {signal_type.replace('_', ' ').title()} ({direction})
Trigger: {reason}
Current Price: ₹{current_price}

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
        )
        result = parse_llm_json(response, {"summary": "", "key_factor": "", "risk": ""})
        summary = result.get("summary", "")
        key = result.get("key_factor", "")
        risk = result.get("risk", "")
        parts = [p for p in [summary, key and f"Key factor: {key}", risk and f"Risk: {risk}"] if p]
        return " ".join(parts)
    except Exception as e:
        logger.warning(f"Failed to enrich signal for {symbol}: {e}")
        return ""


async def run_analysis(
    symbol: str,
    timeframe: str,
    technicals: dict,
    sr: dict,
    fib: dict,
    poc: Optional[float],
    stock_info: dict,
    settings: dict,
) -> dict[str, Any]:
    """
    Full AI analysis for a stock (user-triggered, on-demand).
    Returns structured analysis dict.
    """
    provider = settings.get("llm_provider", "gemini")
    model = settings.get("llm_model", "gemini-2.0-flash")
    api_key = _get_api_key(settings, provider)

    current_price = technicals.get("current_price", "N/A")
    stock_name = stock_info.get("name", symbol)
    sector = stock_info.get("sector", "N/A")
    pe_ratio = stock_info.get("pe_ratio", "N/A")
    market_cap = stock_info.get("market_cap", "N/A")
    timeframe_map = {
        "intraday": "intraday (today's session)",
        "swing": "swing trade (1–2 weeks)",
        "long": "long-term investment (3–12 months)",
        "short": "short-term (1 week to 1 month)",  # legacy
    }
    timeframe_desc = timeframe_map.get(timeframe, "short-term (1 week to 1 month)")

    ma = technicals.get("moving_averages", {})
    macd = technicals.get("macd", {})
    bb = technicals.get("bollinger_bands", {})
    resistance = sr.get("resistance", {})
    support = sr.get("support", {})
    fib_levels = fib.get("levels", {})

    prompt = f"""You are a senior Indian stock market analyst AI. Analyze this stock and provide insights for {timeframe_desc}.

STOCK: {stock_name} ({symbol})
Sector: {sector} | P/E: {pe_ratio} | Market Cap: {market_cap}
Current Price: ₹{current_price}

TECHNICAL INDICATORS:
- RSI(14): {technicals.get('rsi')} ({technicals.get('rsi_signal')})
- ADX: {technicals.get('adx')} (trend strength)
- MACD: Line={macd.get('macd_line')}, Signal={macd.get('signal_line')}, Histogram={macd.get('histogram')} → {macd.get('signal')}
- SMA20={ma.get('sma20')}, SMA50={ma.get('sma50')}, SMA200={ma.get('sma200')}
- Bollinger: {bb.get('signal')} (Upper={bb.get('upper')}, Lower={bb.get('lower')})

SUPPORT / RESISTANCE:
- Pivot: {sr.get('pivot')}, R1={resistance.get('r1')}, R2={resistance.get('r2')}, S1={support.get('s1')}, S2={support.get('s2')}
- Fibonacci 38.2%={fib_levels.get('level_38_2')}, 50%={fib_levels.get('level_50_0')}, 61.8%={fib_levels.get('level_61_8')}
- Volume POC: ₹{poc}

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

    fallback = {
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

    try:
        response = await call_llm(
            provider=provider,
            model=model,
            api_key=api_key,
            prompt=prompt,
            system_message="You are an expert Indian stock market analyst. Analyze objectively. Return ONLY valid JSON.",
        )
        result = parse_llm_json(response, fallback)
        return result
    except Exception as e:
        logger.error(f"AI analysis failed for {symbol}: {e}")
        return fallback

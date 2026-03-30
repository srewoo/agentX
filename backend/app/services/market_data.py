from __future__ import annotations
"""
Market-wide data enrichment — free NSE data that improves signal quality.

Provides:
  1. Corporate actions (dividends, splits, bonuses) — upcoming events affect price
  2. Announcements (board meetings, results) — catalysts for big moves
  3. Block deals — large institutional transactions signal smart money
  4. Options chain analysis (PCR, max pain, unusual OI) — derivative signals
  5. Advance/decline & market breadth — confirm bull/bear regime

All data from NseIndiaApi (3 req/sec, free, no API key).
"""
import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Corporate Actions ────────────────────────────────────────

def _sync_fetch_actions() -> list[dict]:
    """Fetch upcoming corporate actions (dividends, splits, bonuses)."""
    try:
        from nse import NSE
        from pathlib import Path
        nse = NSE(Path("/tmp/agentx_nse"))
        data = nse.actions()
        nse.exit()
        if not data or not isinstance(data, list):
            return []

        results = []
        for item in data:
            results.append({
                "symbol": item.get("symbol"),
                "company": item.get("comp"),
                "action": item.get("subject"),
                "ex_date": item.get("exDate"),
                "record_date": item.get("recDate"),
                "series": item.get("series"),
            })
        return results
    except Exception as e:
        logger.debug("Failed to fetch corporate actions: %s", e)
        return []


async def get_corporate_actions() -> list[dict]:
    """Async: upcoming dividends, splits, bonuses from NSE."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_fetch_actions)


def get_actions_for_symbol(actions: list[dict], symbol: str) -> list[dict]:
    """Filter actions for a specific symbol."""
    return [a for a in actions if a.get("symbol") == symbol]


# ── Announcements ────────────────────────────────────────────

def _sync_fetch_announcements(symbol: Optional[str] = None) -> list[dict]:
    """Fetch recent corporate announcements."""
    try:
        from nse import NSE
        from pathlib import Path
        nse = NSE(Path("/tmp/agentx_nse"))
        data = nse.announcements(symbol=symbol) if symbol else nse.announcements()
        nse.exit()
        if not data or not isinstance(data, list):
            return []

        results = []
        for item in data[:30]:  # Cap at 30 to keep response size manageable
            results.append({
                "symbol": item.get("symbol"),
                "company": item.get("sm_name"),
                "description": item.get("desc"),
                "date": item.get("an_dt"),
                "attachment": item.get("attchmntFile"),
            })
        return results
    except Exception as e:
        logger.debug("Failed to fetch announcements: %s", e)
        return []


async def get_announcements(symbol: Optional[str] = None) -> list[dict]:
    """Async: recent corporate announcements from NSE."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_fetch_announcements, symbol)


# ── Block Deals (Institutional Activity) ─────────────────────

def _sync_fetch_block_deals() -> list[dict]:
    """Fetch today's block deals — large institutional transactions."""
    try:
        from nse import NSE
        from pathlib import Path
        nse = NSE(Path("/tmp/agentx_nse"))
        data = nse.blockDeals()
        nse.exit()
        if not data or not isinstance(data, dict):
            return []

        deals = data.get("data", [])
        results = []
        for deal in deals:
            results.append({
                "symbol": deal.get("symbol"),
                "price": deal.get("lastPrice"),
                "volume": deal.get("totalTradedVolume"),
                "value": deal.get("totalTradedValue"),
                "change_pct": deal.get("pchange"),
            })
        return results
    except Exception as e:
        logger.debug("Failed to fetch block deals: %s", e)
        return []


async def get_block_deals() -> list[dict]:
    """Async: today's block deals from NSE."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_fetch_block_deals)


# ── Options Chain Analysis ───────────────────────────────────

def _sync_analyze_option_chain(symbol: str) -> Optional[dict]:
    """
    Fetch and analyze options chain data for a symbol.
    Returns PCR, max pain, unusual OI buildup signals.
    """
    try:
        from nse import NSE
        from pathlib import Path
        nse = NSE(Path("/tmp/agentx_nse"))
        data = nse.optionChain(symbol)
        nse.exit()

        if not data or not isinstance(data, dict):
            return None

        records = data.get("records", {})
        strikes = records.get("data", [])
        expiry_dates = records.get("expiryDates", [])
        underlying_value = records.get("underlyingValue")

        if not strikes or not underlying_value:
            return None

        # Use nearest NON-EXPIRED expiry (skip today's date which has zeroed-out OI)
        today_str = date.today().strftime("%d-%b-%Y")
        nearest_expiry = None
        for exp in expiry_dates:
            if exp != today_str:
                nearest_expiry = exp
                break
        if not nearest_expiry and expiry_dates:
            nearest_expiry = expiry_dates[0]  # fallback to first available

        total_ce_oi = 0
        total_pe_oi = 0
        total_ce_volume = 0
        total_pe_volume = 0
        max_ce_oi = 0
        max_ce_oi_strike = 0
        max_pe_oi = 0
        max_pe_oi_strike = 0

        for strike_data in strikes:
            ce = strike_data.get("CE", {})
            pe = strike_data.get("PE", {})

            # Only consider nearest expiry
            if nearest_expiry and ce.get("expiryDate") and nearest_expiry not in str(ce.get("expiryDate", "")):
                continue

            ce_oi = ce.get("openInterest", 0) or 0
            pe_oi = pe.get("openInterest", 0) or 0
            ce_vol = ce.get("totalTradedVolume", 0) or 0
            pe_vol = pe.get("totalTradedVolume", 0) or 0

            total_ce_oi += ce_oi
            total_pe_oi += pe_oi
            total_ce_volume += ce_vol
            total_pe_volume += pe_vol

            if ce_oi > max_ce_oi:
                max_ce_oi = ce_oi
                max_ce_oi_strike = ce.get("strikePrice", 0)
            if pe_oi > max_pe_oi:
                max_pe_oi = pe_oi
                max_pe_oi_strike = pe.get("strikePrice", 0)

        # Put-Call Ratio
        pcr_oi = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 0
        pcr_volume = round(total_pe_volume / total_ce_volume, 2) if total_ce_volume > 0 else 0

        # PCR interpretation
        if pcr_oi > 1.3:
            pcr_signal = "Bullish"
            pcr_desc = "High put writing suggests support — bulls in control"
        elif pcr_oi < 0.7:
            pcr_signal = "Bearish"
            pcr_desc = "Low PCR suggests excessive call buying — potential overbought"
        else:
            pcr_signal = "Neutral"
            pcr_desc = "PCR in normal range"

        # Max pain estimation (strike where max OI expires worthless)
        # Simplified: midpoint between max CE OI strike and max PE OI strike
        max_pain = round((max_ce_oi_strike + max_pe_oi_strike) / 2) if max_ce_oi_strike and max_pe_oi_strike else None

        # Unusual OI change detection
        unusual_ce_strikes = []
        unusual_pe_strikes = []
        for strike_data in strikes:
            ce = strike_data.get("CE", {})
            pe = strike_data.get("PE", {})
            ce_oi_chg = abs(ce.get("changeinOpenInterest", 0) or 0)
            pe_oi_chg = abs(pe.get("changeinOpenInterest", 0) or 0)
            if ce_oi_chg > total_ce_oi * 0.05 and ce_oi_chg > 0:  # >5% of total OI
                unusual_ce_strikes.append({
                    "strike": ce.get("strikePrice"),
                    "oi_change": ce.get("changeinOpenInterest"),
                    "iv": ce.get("impliedVolatility"),
                })
            if pe_oi_chg > total_pe_oi * 0.05 and pe_oi_chg > 0:
                unusual_pe_strikes.append({
                    "strike": pe.get("strikePrice"),
                    "oi_change": pe.get("changeinOpenInterest"),
                    "iv": pe.get("impliedVolatility"),
                })

        return {
            "symbol": symbol,
            "underlying_value": underlying_value,
            "nearest_expiry": nearest_expiry,
            "pcr_oi": pcr_oi,
            "pcr_volume": pcr_volume,
            "pcr_signal": pcr_signal,
            "pcr_description": pcr_desc,
            "max_pain": max_pain,
            "max_ce_oi_strike": max_ce_oi_strike,
            "max_pe_oi_strike": max_pe_oi_strike,
            "total_ce_oi": total_ce_oi,
            "total_pe_oi": total_pe_oi,
            "unusual_ce_activity": unusual_ce_strikes[:3],
            "unusual_pe_activity": unusual_pe_strikes[:3],
        }
    except Exception as e:
        logger.debug("Option chain analysis failed for %s: %s", symbol, e)
        return None


async def get_option_chain_analysis(symbol: str) -> Optional[dict]:
    """Async: options chain analysis with PCR, max pain, unusual OI."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_analyze_option_chain, symbol)


# ── Market Breadth (Advance/Decline from Status) ─────────────

def _sync_fetch_market_breadth() -> Optional[dict]:
    """Derive market breadth from NSE status endpoint."""
    try:
        from nse import NSE
        from pathlib import Path
        nse = NSE(Path("/tmp/agentx_nse"))
        status = nse.status()
        nse.exit()

        if not status:
            return None

        # Extract NIFTY 50 info from status
        nifty = None
        for s in status:
            if s.get("index") == "NIFTY 50":
                nifty = s
                break

        if not nifty:
            return None

        return {
            "index": "NIFTY 50",
            "last": nifty.get("last"),
            "variation": nifty.get("variation"),
            "percent_change": nifty.get("percentChange"),
            "market_status": nifty.get("marketStatus"),
            "trade_date": nifty.get("tradeDate"),
        }
    except Exception as e:
        logger.debug("Market breadth fetch failed: %s", e)
        return None


async def get_market_breadth() -> Optional[dict]:
    """Async: market breadth data."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_fetch_market_breadth)


# ── Aggregate context for signal enrichment ──────────────────

async def get_market_context(symbol: Optional[str] = None) -> dict[str, Any]:
    """
    Gather all available market context for signal enrichment.
    Called by the orchestrator and LLM analyst to provide richer context.
    """
    context: dict[str, Any] = {}

    # Run all fetches in parallel
    tasks = {
        "actions": get_corporate_actions(),
        "block_deals": get_block_deals(),
        "breadth": get_market_breadth(),
    }

    # Only fetch options for FnO-eligible symbols
    if symbol:
        tasks["options"] = get_option_chain_analysis(symbol)
        tasks["announcements"] = get_announcements(symbol)

    results = {}
    for key, coro in tasks.items():
        try:
            results[key] = await coro
        except Exception as e:
            logger.debug("Market context fetch failed for %s: %s", key, e)
            results[key] = None

    context["corporate_actions"] = results.get("actions", [])
    context["block_deals"] = results.get("block_deals", [])
    context["market_breadth"] = results.get("breadth")
    context["options_analysis"] = results.get("options")
    context["announcements"] = results.get("announcements", [])

    # Filter actions for the specific symbol
    if symbol and context["corporate_actions"]:
        context["symbol_actions"] = get_actions_for_symbol(context["corporate_actions"], symbol)
    else:
        context["symbol_actions"] = []

    # Check if symbol has block deal activity today
    if symbol and context["block_deals"]:
        context["symbol_block_deals"] = [
            d for d in context["block_deals"] if d.get("symbol") == symbol
        ]
    else:
        context["symbol_block_deals"] = []

    return context

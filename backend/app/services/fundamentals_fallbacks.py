from __future__ import annotations
"""
Free Indian fundamentals fallback chain. Yahoo / yfinance throttles hard,
so we layer cheaper, more reliable sources on top:

  1. `nse.quote()`            — sector PE + symbol PE + industry, no scrape.
  2. screener.in HTML scrape  — full ratio set (PE, P/B, ROE, ROCE,
                                dividend yield, debt/equity, growth).

Each helper returns a *partial* fundamentals dict shaped like the canonical
`fundamentals.py` output — `merge_fundamentals` fills missing fields on the
primary without overwriting non-null values.

Why partial: every source covers a different subset. Yahoo has the broadest
coverage when it works; NSE is bulletproof but limited; screener.in has the
ratios but no insider/institutional ownership. Composing them yields the
densest output a free pipeline can produce.
"""
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── tiny utilities ────────────────────────────────────────────────────────

def _f(v: Any) -> Optional[float]:
    """Permissive number coercion. Returns None for "—", "n/a", empty etc."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return None if (v != v) else float(v)  # NaN guard
    s = str(v).strip()
    if not s or s in {"—", "-", "n/a", "N/A", "NA", "Nil", "—%"}:
        return None
    s = s.replace(",", "").replace("₹", "").replace("%", "").strip()
    # Indian Cr / Lakh suffixes are common on screener.
    mult = 1.0
    if s.endswith("Cr."):
        mult = 1e7
        s = s[:-3].strip()
    elif s.endswith("Cr"):
        mult = 1e7
        s = s[:-2].strip()
    elif s.endswith("L"):
        mult = 1e5
        s = s[:-1].strip()
    try:
        return float(s) * mult
    except ValueError:
        return None


def _empty_partial() -> dict[str, Any]:
    return {
        "valuation": {},
        "growth": {},
        "profitability": {},
        "financial_health": {},
        "dividends": {},
        "ownership": {},
        "earnings": {},
    }


# ── source 1: NSE quote ───────────────────────────────────────────────────

def fetch_nse_quote(symbol: str) -> Optional[dict[str, Any]]:
    """Pull symbol PE, sector PE, industry + price info from `nse.quote`.

    Free, no auth, very reliable. Doesn't include ROE / D/E / margins —
    those need screener.in.
    """
    try:
        from nse import NSE
        from pathlib import Path

        nse = NSE(Path("/tmp/agentx_nse"))
        try:
            q = nse.quote(symbol)
        finally:
            nse.exit()
    except Exception as e:
        logger.debug("nse.quote failed for %s: %s", symbol, e)
        return None

    if not isinstance(q, dict):
        return None

    metadata = q.get("metadata") or {}
    price_info = q.get("priceInfo") or {}
    sec_info = q.get("securityInfo") or {}

    pe = _f(metadata.get("pdSymbolPe"))
    out = _empty_partial()
    out["valuation"]["pe"] = pe

    # NSE's `industry` is the granular label users want ("Refineries &
    # Marketing", "Public Sector Bank") — use it as the sector value.
    # `pdSectorInd` is the broad index ("NIFTY 50" / "NIFTY BANK") which is
    # only useful when it actually names an industry index — otherwise it
    # collapses to a meaningless "50".
    industry = metadata.get("industry")
    out["industry"] = industry
    raw_sector = metadata.get("pdSectorInd") or ""
    is_industry_index = bool(raw_sector) and not any(
        raw_sector.upper().endswith(f" {n}") for n in ("50", "100", "200", "500")
    )
    if industry:
        out["sector"] = industry
    elif is_industry_index:
        out["sector"] = (
            raw_sector[len("NIFTY"):].strip() if raw_sector.upper().startswith("NIFTY") else raw_sector
        )
    out["nse_sector_pe"] = _f(metadata.get("pdSectorPe"))

    # Compute market cap = lastPrice × issuedSize. Pure derived field but
    # useful for sector-cap-band classification later.
    last = _f(price_info.get("lastPrice"))
    issued = _f(sec_info.get("issuedSize"))
    if last and issued:
        out["market_cap"] = last * issued

    return out


# ── source 2: screener.in HTML scrape ─────────────────────────────────────

_SCREENER_BASE = "https://www.screener.in/company"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36 agentX/1.0"
)


def fetch_screener_in(symbol: str) -> Optional[dict[str, Any]]:
    """Scrape Screener.in for full ratio set.

    Tries `/company/<sym>/consolidated/` first (preferred for groups),
    falls back to `/company/<sym>/`. Cached at the router layer (6h fresh,
    7d last-good), so each symbol hits screener at most a few times a day.
    """
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError:
        return None

    urls = [
        f"{_SCREENER_BASE}/{symbol}/consolidated/",
        f"{_SCREENER_BASE}/{symbol}/",
    ]
    html: Optional[str] = None
    for url in urls:
        try:
            r = requests.get(url, headers={"User-Agent": _UA}, timeout=15)
            if r.status_code == 200 and "<title>" in r.text:
                html = r.text
                break
        except Exception as e:
            logger.debug("screener.in fetch failed for %s (%s): %s", symbol, url, e)

    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")
    ratios: dict[str, Optional[float]] = {}
    top = soup.find("ul", id="top-ratios")
    if top:
        for li in top.find_all("li"):
            name_el = li.find("span", class_="name")
            val_el = li.find("span", class_="nowrap value") or li.find("span", class_="number")
            if not name_el:
                continue
            key = name_el.get_text(strip=True)
            val_text = val_el.get_text(" ", strip=True) if val_el else ""
            ratios[key] = _f(val_text)

    if not ratios:
        return None

    market_cap = ratios.get("Market Cap")
    pe = ratios.get("Stock P/E")
    book_value = ratios.get("Book Value")
    current_price = ratios.get("Current Price")
    pb = (current_price / book_value) if (current_price and book_value and book_value > 0) else None
    div_yield = ratios.get("Dividend Yield")  # already a percent on screener
    roe = ratios.get("ROE")
    roce = ratios.get("ROCE")
    eps = ratios.get("EPS")
    debt_ratio = ratios.get("Debt to equity") or ratios.get("Debt / Equity")
    profit_margin = ratios.get("OPM") or ratios.get("Operating Profit Margin")
    rev_growth = ratios.get("Sales growth") or ratios.get("Revenue growth")
    eps_growth = ratios.get("EPS growth") or ratios.get("Profit growth")

    out = _empty_partial()
    out["valuation"]["pe"] = pe
    out["valuation"]["pb"] = pb
    # screener percentages need conversion to fraction for our internal shape.
    if roe is not None:
        out["profitability"]["roe"] = roe / 100.0
    if profit_margin is not None:
        out["profitability"]["profit_margin"] = profit_margin / 100.0
    if roce is not None:
        out["profitability"]["roa"] = roce / 100.0  # close enough proxy when ROA missing
    if debt_ratio is not None:
        out["financial_health"]["debt_to_equity"] = debt_ratio
    if div_yield is not None:
        out["dividends"]["dividend_yield"] = div_yield / 100.0
        out["dividends"]["yield"] = div_yield / 100.0
    if rev_growth is not None:
        out["growth"]["revenue_growth"] = rev_growth / 100.0
    if eps_growth is not None:
        out["growth"]["earnings_growth"] = eps_growth / 100.0
    if eps is not None:
        out["earnings"]["trailing_eps"] = eps
    if market_cap is not None:
        out["market_cap"] = market_cap

    out["source"] = "screener.in"
    return out


# ── merge ─────────────────────────────────────────────────────────────────

_NESTED_KEYS = ("valuation", "growth", "profitability", "financial_health",
                "dividends", "ownership", "earnings")


def merge_fundamentals(primary: dict[str, Any], *fallbacks: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Fill `None` / missing fields in `primary` from each fallback in order.

    Existing non-null values on `primary` are preserved — fallbacks only
    contribute where the primary source has nothing. This means yfinance
    keeps winning when it actually returns data, and we only "borrow" for
    the fields it left empty.
    """
    out = dict(primary)
    for src in fallbacks:
        if not src:
            continue
        for nested in _NESTED_KEYS:
            target = dict(out.get(nested) or {})
            for k, v in (src.get(nested) or {}).items():
                if v is None:
                    continue
                if target.get(k) is None:
                    target[k] = v
            out[nested] = target
        # Top-level scalars (sector / industry / market_cap / etc.).
        for k in ("sector", "industry", "market_cap", "nse_sector_pe"):
            if out.get(k) in (None, "") and src.get(k) not in (None, ""):
                out[k] = src.get(k)

    # If we filled anything material, drop the "rate-limit" error that the
    # primary set when it came back empty — the UI should now show data.
    fields_present = any(
        (out.get(nested) or {}).get(k) is not None
        for nested in _NESTED_KEYS
        for k in (out.get(nested) or {})
    )
    if fields_present and out.get("error"):
        out.pop("error", None)
    return out

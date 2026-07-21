from __future__ import annotations
"""1.1 — Widen the forward-test funnel to 200+ liquid NSE names.

The forward test only produces evidence as fast as it produces trades. Scanning
~40 majors starves the funnel; at ~8 closed trades we're a year away from the
300-trade bar. Widening the *candidate* universe (while keeping every entry
guardrail and lowering per-trade notional so total exposure is unchanged) is the
cheapest way to raise cadence toward ≥25 trades/week.

This module supplies the wider universe and a **liquidity floor** so we never
add names too thin to fill a paper order honestly:

  * ``LIQUID_UNIVERSE`` — MAJOR_STOCKS plus a curated large/mid-cap extension,
    all high-turnover NSE names. Deduplicated, sector-tagged.
  * ``compute_adv_cr(df)`` — average daily traded VALUE in ₹ crore from OHLCV
    (mean of close×volume over the recent window). This is the real liquidity
    measure; share count alone is meaningless across price levels.
  * ``passes_liquidity_floor(adv_cr)`` — ADV ≥ ₹5cr by default.

Membership in the curated list is a *proxy* for liquidity (every name is a
heavily-traded large/mid cap); when live OHLCV is available the ADV floor is the
authoritative check and can prune a name that has gone thin.
"""
import logging
from typing import Optional

from app.services.data_fetcher import MAJOR_STOCKS

logger = logging.getLogger(__name__)

DEFAULT_ADV_FLOOR_CR = 5.0   # ₹5 crore average daily traded value
DEFAULT_ADV_WINDOW = 20      # trading days to average over

# Curated liquid large/mid-cap extension beyond MAJOR_STOCKS. All are
# high-turnover NSE names (ADV comfortably above the floor in normal markets);
# the live ADV check still governs at scan time.
_EXTENSION = [
    ("DLF", "Realty"), ("GODREJPROP", "Realty"), ("OBEROIRLTY", "Realty"),
    ("LODHA", "Realty"), ("PHOENIXLTD", "Realty"),
    ("PIDILITIND", "Chemicals"), ("SRF", "Chemicals"), ("PIIND", "Chemicals"),
    ("DEEPAKNTR", "Chemicals"), ("AARTIIND", "Chemicals"), ("NAVINFLUOR", "Chemicals"),
    ("TATACHEM", "Chemicals"), ("UPL", "Chemicals"),
    ("LTIM", "IT"), ("PERSISTENT", "IT"), ("COFORGE", "IT"), ("MPHASIS", "IT"),
    ("LTTS", "IT"), ("OFSS", "IT"),
    ("MOTHERSON", "Auto"), ("BOSCHLTD", "Auto"), ("TVSMOTOR", "Auto"),
    ("BAJAJ-AUTO", "Auto"), ("ASHOKLEY", "Auto"), ("MRF", "Auto"),
    ("BALKRISIND", "Auto"), ("BHARATFORG", "Auto"), ("EXIDEIND", "Auto"),
    ("LUPIN", "Pharma"), ("AUROPHARMA", "Pharma"), ("TORNTPHARM", "Pharma"),
    ("ALKEM", "Pharma"), ("BIOCON", "Pharma"), ("ZYDUSLIFE", "Pharma"),
    ("IPCALAB", "Pharma"), ("GLENMARK", "Pharma"), ("LAURUSLABS", "Pharma"),
    ("MANKIND", "Pharma"), ("MAXHEALTH", "Healthcare"), ("FORTIS", "Healthcare"),
    ("BAJAJHLDNG", "Finance"), ("CHOLAFIN", "Finance"), ("MUTHOOTFIN", "Finance"),
    ("SHRIRAMFIN", "Finance"), ("SBICARD", "Finance"), ("PFC", "Finance"),
    ("RECLTD", "Finance"), ("LICHSGFIN", "Finance"), ("M&MFIN", "Finance"),
    ("IDFCFIRSTB", "Banking"), ("AUBANK", "Banking"), ("FEDERALBNK", "Banking"),
    ("BANDHANBNK", "Banking"), ("RBLBANK", "Banking"),
    ("ICICIPRULI", "Insurance"), ("ICICIGI", "Insurance"), ("LICI", "Insurance"),
    ("DABUR", "FMCG"), ("MARICO", "FMCG"), ("GODREJCP", "FMCG"),
    ("COLPAL", "FMCG"), ("UBL", "FMCG"), ("VBL", "FMCG"), ("PGHH", "FMCG"),
    ("HAVELLS", "Consumer"), ("VOLTAS", "Consumer"), ("DIXON", "Consumer"),
    ("CROMPTON", "Consumer"), ("BERGEPAINT", "Consumer"), ("PAGEIND", "Consumer"),
    ("DMART", "Retail"), ("NYKAA", "Retail"), ("JUBLFOOD", "Retail"),
    ("INDIGO", "Aviation"), ("IRCTC", "Transport"), ("CONCOR", "Transport"),
    ("GAIL", "Energy"), ("IOC", "Energy"), ("HINDPETRO", "Energy"),
    ("PETRONET", "Energy"), ("IGL", "Energy"), ("ATGL", "Energy"),
    ("TATAPOWER", "Power"), ("ADANIGREEN", "Power"), ("ADANIENSOL", "Power"),
    ("JSWENERGY", "Power"), ("NHPC", "Power"), ("SJVN", "Power"),
    ("VEDL", "Metals"), ("NMDC", "Metals"), ("SAIL", "Metals"),
    ("JINDALSTEL", "Metals"), ("APLAPOLLO", "Metals"), ("HINDZINC", "Metals"),
    ("NATIONALUM", "Metals"), ("JSL", "Metals"),
    ("AMBUJACEM", "Cement"), ("SHREECEM", "Cement"), ("ACC", "Cement"),
    ("DALBHARAT", "Cement"), ("RAMCOCEM", "Cement"),
    ("ABB", "Capital Goods"), ("SIEMENS", "Capital Goods"), ("CGPOWER", "Capital Goods"),
    ("POLYCAB", "Capital Goods"), ("CUMMINSIND", "Capital Goods"),
    ("SUPREMEIND", "Capital Goods"), ("ASTRAL", "Capital Goods"),
    ("PAYTM", "Fintech"), ("POLICYBZR", "Fintech"), ("ANGELONE", "Fintech"),
    ("CDSL", "Fintech"), ("BSE", "Fintech"), ("MCX", "Fintech"),
    ("INDHOTEL", "Consumer"), ("JIOFIN", "Finance"), ("ADANIPOWER", "Power"),
    ("YESBANK", "Banking"), ("IDEA", "Telecom"), ("IRFC", "Finance"),
    ("MAZDOCK", "Defense"), ("COCHINSHIP", "Defense"), ("BDL", "Defense"),
    ("SOLARINDS", "Defense"), ("OIL", "Energy"), ("MFSL", "Insurance"),
]


def _build_universe() -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for s in MAJOR_STOCKS:
        sym = s["symbol"]
        if sym.startswith("^") or sym in seen:
            continue
        seen.add(sym)
        out.append({"symbol": sym, "sector": s.get("sector", "Unknown")})
    for sym, sector in _EXTENSION:
        if sym in seen:
            continue
        seen.add(sym)
        out.append({"symbol": sym, "sector": sector})
    return out


LIQUID_UNIVERSE: list[dict] = _build_universe()


def liquid_symbols() -> list[str]:
    """Just the symbols of the wide liquid universe (200+ names)."""
    return [s["symbol"] for s in LIQUID_UNIVERSE]


def compute_adv_cr(df, window: int = DEFAULT_ADV_WINDOW) -> Optional[float]:
    """Average daily traded value in ₹ crore over the recent ``window`` bars.

    ADV = mean(Close × Volume) over the last ``window`` days, ÷ 1e7 (crore).
    Returns None if the frame lacks the columns or has no usable rows.
    """
    if df is None or len(df) == 0:
        return None
    try:
        cols = {c.lower(): c for c in df.columns}
        close_c, vol_c = cols.get("close"), cols.get("volume")
        if not close_c or not vol_c:
            return None
        recent = df.tail(window)
        value = (recent[close_c].astype(float) * recent[vol_c].astype(float))
        value = value[value > 0]
        if len(value) == 0:
            return None
        return float(value.mean()) / 1e7
    except Exception as e:
        logger.debug("compute_adv_cr failed: %s", e)
        return None


def passes_liquidity_floor(adv_cr: Optional[float], floor_cr: float = DEFAULT_ADV_FLOOR_CR) -> bool:
    """True if ADV clears the floor. Unknown ADV (None) fails CLOSED — a name we
    can't measure is not admitted to the widened funnel."""
    return adv_cr is not None and adv_cr >= floor_cr

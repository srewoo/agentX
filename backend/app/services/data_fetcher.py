from __future__ import annotations
"""
Market data fetching via yfinance.
Forked from FinSight/backend/server.py (resilient_fetch_history + stock list).
"""
import asyncio
import logging
from typing import Any
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


def resilient_fetch_history(symbol: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
    """
    Fetch OHLCV history for a symbol from yfinance.
    Handles .NS suffix, short-period fallback, and empty DataFrame gracefully.
    """
    if symbol.startswith("^") or symbol.endswith(".NS") or symbol.endswith(".BO") or "=" in symbol:
        yf_sym = symbol
    else:
        yf_sym = f"{symbol}.NS"

    hist = pd.DataFrame()

    try:
        hist = yf.Ticker(yf_sym).history(period=period, interval=interval)
    except Exception as e:
        logger.warning(f"yfinance failed for {yf_sym}: {e}")

    # Short-period fallback: "5d" sometimes returns empty for Indian stocks on weekends/holidays
    if hist.empty and period == "5d" and interval == "1d":
        try:
            hist_1mo = yf.Ticker(yf_sym).history(period="1mo", interval="1d")
            if not hist_1mo.empty:
                hist = hist_1mo.tail(5)
        except Exception as e:
            logger.warning(f"yfinance 1mo fallback failed for {yf_sym}: {e}")

    return hist


async def async_fetch_history(symbol: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
    """Async wrapper around resilient_fetch_history."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, resilient_fetch_history, symbol, period, interval)


def get_stock_info(symbol: str) -> dict[str, Any]:
    """Fetch stock metadata (name, sector, PE, market cap) via yfinance."""
    try:
        yf_sym = symbol if (symbol.startswith("^") or "." in symbol) else f"{symbol}.NS"
        ticker = yf.Ticker(yf_sym)
        info = ticker.info
        return {
            "name": info.get("longName", symbol),
            "sector": info.get("sector", "N/A"),
            "industry": info.get("industry", "N/A"),
            "pe_ratio": info.get("trailingPE"),
            "market_cap": info.get("marketCap"),
            "currency": info.get("currency", "INR"),
        }
    except Exception as e:
        logger.warning(f"Could not fetch info for {symbol}: {e}")
        return {"name": symbol, "sector": "N/A"}


# Major Indian stocks for scanning (NIFTY 50 + broader universe)
MAJOR_STOCKS = [
    # NIFTY 50
    {"symbol": "RELIANCE", "name": "Reliance Industries", "sector": "Energy"},
    {"symbol": "TCS", "name": "Tata Consultancy Services", "sector": "IT"},
    {"symbol": "HDFCBANK", "name": "HDFC Bank", "sector": "Banking"},
    {"symbol": "INFY", "name": "Infosys", "sector": "IT"},
    {"symbol": "ICICIBANK", "name": "ICICI Bank", "sector": "Banking"},
    {"symbol": "HINDUNILVR", "name": "Hindustan Unilever", "sector": "FMCG"},
    {"symbol": "SBIN", "name": "State Bank of India", "sector": "Banking"},
    {"symbol": "BHARTIARTL", "name": "Bharti Airtel", "sector": "Telecom"},
    {"symbol": "ITC", "name": "ITC Limited", "sector": "FMCG"},
    {"symbol": "KOTAKBANK", "name": "Kotak Mahindra Bank", "sector": "Banking"},
    {"symbol": "LT", "name": "Larsen & Toubro", "sector": "Infrastructure"},
    {"symbol": "AXISBANK", "name": "Axis Bank", "sector": "Banking"},
    {"symbol": "WIPRO", "name": "Wipro", "sector": "IT"},
    {"symbol": "ASIANPAINT", "name": "Asian Paints", "sector": "Consumer"},
    {"symbol": "MARUTI", "name": "Maruti Suzuki", "sector": "Auto"},
    {"symbol": "TATAMOTORS", "name": "Tata Motors", "sector": "Auto"},
    {"symbol": "SUNPHARMA", "name": "Sun Pharmaceutical", "sector": "Pharma"},
    {"symbol": "BAJFINANCE", "name": "Bajaj Finance", "sector": "Finance"},
    {"symbol": "TITAN", "name": "Titan Company", "sector": "Consumer"},
    {"symbol": "NESTLEIND", "name": "Nestle India", "sector": "FMCG"},
    {"symbol": "TECHM", "name": "Tech Mahindra", "sector": "IT"},
    {"symbol": "HCLTECH", "name": "HCL Technologies", "sector": "IT"},
    {"symbol": "ULTRACEMCO", "name": "UltraTech Cement", "sector": "Cement"},
    {"symbol": "POWERGRID", "name": "Power Grid Corporation", "sector": "Power"},
    {"symbol": "NTPC", "name": "NTPC Limited", "sector": "Power"},
    {"symbol": "ONGC", "name": "Oil & Natural Gas Corp", "sector": "Energy"},
    {"symbol": "TATASTEEL", "name": "Tata Steel", "sector": "Metals"},
    {"symbol": "JSWSTEEL", "name": "JSW Steel", "sector": "Metals"},
    {"symbol": "ADANIENT", "name": "Adani Enterprises", "sector": "Conglomerate"},
    {"symbol": "ADANIPORTS", "name": "Adani Ports", "sector": "Infrastructure"},
    {"symbol": "COALINDIA", "name": "Coal India", "sector": "Mining"},
    {"symbol": "DRREDDY", "name": "Dr Reddys Laboratories", "sector": "Pharma"},
    {"symbol": "CIPLA", "name": "Cipla", "sector": "Pharma"},
    {"symbol": "EICHERMOT", "name": "Eicher Motors", "sector": "Auto"},
    {"symbol": "HEROMOTOCO", "name": "Hero MotoCorp", "sector": "Auto"},
    {"symbol": "BAJAJFINSV", "name": "Bajaj Finserv", "sector": "Finance"},
    {"symbol": "BRITANNIA", "name": "Britannia Industries", "sector": "FMCG"},
    {"symbol": "DIVISLAB", "name": "Divis Laboratories", "sector": "Pharma"},
    {"symbol": "GRASIM", "name": "Grasim Industries", "sector": "Cement"},
    {"symbol": "APOLLOHOSP", "name": "Apollo Hospitals", "sector": "Healthcare"},
    {"symbol": "HDFCLIFE", "name": "HDFC Life Insurance", "sector": "Insurance"},
    {"symbol": "SBILIFE", "name": "SBI Life Insurance", "sector": "Insurance"},
    {"symbol": "TATACONSUM", "name": "Tata Consumer Products", "sector": "FMCG"},
    {"symbol": "INDUSINDBK", "name": "IndusInd Bank", "sector": "Banking"},
    {"symbol": "HINDALCO", "name": "Hindalco Industries", "sector": "Metals"},
    {"symbol": "BPCL", "name": "Bharat Petroleum", "sector": "Energy"},
    {"symbol": "ZOMATO", "name": "Zomato", "sector": "Consumer"},
    {"symbol": "TRENT", "name": "Trent", "sector": "Consumer"},
    {"symbol": "BEL", "name": "Bharat Electronics", "sector": "Defense"},
    {"symbol": "HAL", "name": "Hindustan Aeronautics", "sector": "Defense"},
    # PSU Banks
    {"symbol": "PNB", "name": "Punjab National Bank", "sector": "Banking"},
    {"symbol": "BANKBARODA", "name": "Bank of Baroda", "sector": "Banking"},
    {"symbol": "CANBK", "name": "Canara Bank", "sector": "Banking"},
    {"symbol": "UNIONBANK", "name": "Union Bank of India", "sector": "Banking"},
    {"symbol": "INDIANB", "name": "Indian Bank", "sector": "Banking"},
    {"symbol": "BANKINDIA", "name": "Bank of India", "sector": "Banking"},
    {"symbol": "IOB", "name": "Indian Overseas Bank", "sector": "Banking"},
    {"symbol": "CENTRALBK", "name": "Central Bank of India", "sector": "Banking"},
    {"symbol": "MAHABANK", "name": "Bank of Maharashtra", "sector": "Banking"},
    {"symbol": "PSB", "name": "Punjab & Sind Bank", "sector": "Banking"},
    # Private Banks
    {"symbol": "FEDERALBNK", "name": "Federal Bank", "sector": "Banking"},
    {"symbol": "BANDHANBNK", "name": "Bandhan Bank", "sector": "Banking"},
    {"symbol": "IDFCFIRSTB", "name": "IDFC First Bank", "sector": "Banking"},
    {"symbol": "RBLBANK", "name": "RBL Bank", "sector": "Banking"},
    {"symbol": "YESBANK", "name": "Yes Bank", "sector": "Banking"},
    {"symbol": "KARURVYSYA", "name": "Karur Vysya Bank", "sector": "Banking"},
    {"symbol": "CSBBANK", "name": "CSB Bank", "sector": "Banking"},
    # IT / Tech
    {"symbol": "MPHASIS", "name": "Mphasis", "sector": "IT"},
    {"symbol": "LTIM", "name": "LTIMindtree", "sector": "IT"},
    {"symbol": "PERSISTENT", "name": "Persistent Systems", "sector": "IT"},
    {"symbol": "COFORGE", "name": "Coforge", "sector": "IT"},
    {"symbol": "OFSS", "name": "Oracle Financial Services", "sector": "IT"},
    {"symbol": "KPITTECH", "name": "KPIT Technologies", "sector": "IT"},
    {"symbol": "TATAELXSI", "name": "Tata Elxsi", "sector": "IT"},
    {"symbol": "CYIENT", "name": "Cyient", "sector": "IT"},
    {"symbol": "MASTEK", "name": "Mastek", "sector": "IT"},
    # Auto & Auto Ancillary
    {"symbol": "BAJAJ-AUTO", "name": "Bajaj Auto", "sector": "Auto"},
    {"symbol": "TVSMOTOR", "name": "TVS Motor Company", "sector": "Auto"},
    {"symbol": "ASHOKLEY", "name": "Ashok Leyland", "sector": "Auto"},
    {"symbol": "M&M", "name": "Mahindra & Mahindra", "sector": "Auto"},
    {"symbol": "BOSCHLTD", "name": "Bosch", "sector": "Auto Ancillary"},
    {"symbol": "MOTHERSON", "name": "Samvardhana Motherson", "sector": "Auto Ancillary"},
    {"symbol": "BALKRISIND", "name": "Balkrishna Industries", "sector": "Auto Ancillary"},
    {"symbol": "MRF", "name": "MRF", "sector": "Auto Ancillary"},
    {"symbol": "APOLLOTYRE", "name": "Apollo Tyres", "sector": "Auto Ancillary"},
    # Pharma / Healthcare
    {"symbol": "LUPIN", "name": "Lupin", "sector": "Pharma"},
    {"symbol": "AUROPHARMA", "name": "Aurobindo Pharma", "sector": "Pharma"},
    {"symbol": "TORNTPHARM", "name": "Torrent Pharmaceuticals", "sector": "Pharma"},
    {"symbol": "ALKEM", "name": "Alkem Laboratories", "sector": "Pharma"},
    {"symbol": "GLENMARK", "name": "Glenmark Pharmaceuticals", "sector": "Pharma"},
    {"symbol": "ABBOTINDIA", "name": "Abbott India", "sector": "Pharma"},
    {"symbol": "IPCA", "name": "IPCA Laboratories", "sector": "Pharma"},
    {"symbol": "MAXHEALTH", "name": "Max Healthcare", "sector": "Healthcare"},
    {"symbol": "FORTIS", "name": "Fortis Healthcare", "sector": "Healthcare"},
    {"symbol": "METROPOLIS", "name": "Metropolis Healthcare", "sector": "Healthcare"},
    # FMCG / Consumer
    {"symbol": "DABUR", "name": "Dabur India", "sector": "FMCG"},
    {"symbol": "MARICO", "name": "Marico", "sector": "FMCG"},
    {"symbol": "COLPAL", "name": "Colgate Palmolive India", "sector": "FMCG"},
    {"symbol": "GODREJCP", "name": "Godrej Consumer Products", "sector": "FMCG"},
    {"symbol": "EMAMILTD", "name": "Emami", "sector": "FMCG"},
    {"symbol": "PGHH", "name": "Procter & Gamble Hygiene", "sector": "FMCG"},
    # Energy / Oil & Gas
    {"symbol": "IOC", "name": "Indian Oil Corporation", "sector": "Energy"},
    {"symbol": "HINDPETRO", "name": "HPCL", "sector": "Energy"},
    {"symbol": "GAIL", "name": "GAIL India", "sector": "Energy"},
    {"symbol": "PETRONET", "name": "Petronet LNG", "sector": "Energy"},
    {"symbol": "OIL", "name": "Oil India", "sector": "Energy"},
    {"symbol": "IGL", "name": "Indraprastha Gas", "sector": "Energy"},
    {"symbol": "MGL", "name": "Mahanagar Gas", "sector": "Energy"},
    # Infrastructure / Construction
    {"symbol": "LTTS", "name": "L&T Technology Services", "sector": "IT"},
    {"symbol": "LICI", "name": "Life Insurance Corporation", "sector": "Insurance"},
    {"symbol": "IRFC", "name": "Indian Railway Finance Corp", "sector": "Finance"},
    {"symbol": "RVNL", "name": "Rail Vikas Nigam", "sector": "Infrastructure"},
    {"symbol": "IRCTC", "name": "IRCTC", "sector": "Infrastructure"},
    {"symbol": "HUDCO", "name": "HUDCO", "sector": "Finance"},
    {"symbol": "PFC", "name": "Power Finance Corporation", "sector": "Finance"},
    {"symbol": "RECLTD", "name": "REC Limited", "sector": "Finance"},
    # Metals & Mining
    {"symbol": "VEDL", "name": "Vedanta", "sector": "Metals"},
    {"symbol": "NMDC", "name": "NMDC", "sector": "Mining"},
    {"symbol": "SAIL", "name": "Steel Authority of India", "sector": "Metals"},
    {"symbol": "NATIONALUM", "name": "National Aluminium", "sector": "Metals"},
    {"symbol": "HINDCOPPER", "name": "Hindustan Copper", "sector": "Metals"},
    {"symbol": "MOIL", "name": "MOIL", "sector": "Mining"},
    # Finance / NBFC
    {"symbol": "BAJAJHLDNG", "name": "Bajaj Holdings", "sector": "Finance"},
    {"symbol": "CHOLAFIN", "name": "Cholamandalam Finance", "sector": "Finance"},
    {"symbol": "MUTHOOTFIN", "name": "Muthoot Finance", "sector": "Finance"},
    {"symbol": "MANAPPURAM", "name": "Manappuram Finance", "sector": "Finance"},
    {"symbol": "M&MFIN", "name": "M&M Financial Services", "sector": "Finance"},
    {"symbol": "SHRIRAMFIN", "name": "Shriram Finance", "sector": "Finance"},
    {"symbol": "LICHSGFIN", "name": "LIC Housing Finance", "sector": "Finance"},
    # Insurance
    {"symbol": "ICICIPRULI", "name": "ICICI Prudential Life", "sector": "Insurance"},
    {"symbol": "ICICIGI", "name": "ICICI Lombard General Insurance", "sector": "Insurance"},
    {"symbol": "NIACL", "name": "New India Assurance", "sector": "Insurance"},
    {"symbol": "STARHEALTH", "name": "Star Health Insurance", "sector": "Insurance"},
    # Cement
    {"symbol": "ACC", "name": "ACC", "sector": "Cement"},
    {"symbol": "AMBUJACEM", "name": "Ambuja Cements", "sector": "Cement"},
    {"symbol": "SHREECEM", "name": "Shree Cement", "sector": "Cement"},
    {"symbol": "RAMCOCEM", "name": "Ramco Cements", "sector": "Cement"},
    # Telecom
    {"symbol": "IDEA", "name": "Vodafone Idea", "sector": "Telecom"},
    {"symbol": "TATACOMM", "name": "Tata Communications", "sector": "Telecom"},
    # Real Estate
    {"symbol": "DLF", "name": "DLF", "sector": "Real Estate"},
    {"symbol": "GODREJPROP", "name": "Godrej Properties", "sector": "Real Estate"},
    {"symbol": "OBEROIRLTY", "name": "Oberoi Realty", "sector": "Real Estate"},
    {"symbol": "PRESTIGE", "name": "Prestige Estates", "sector": "Real Estate"},
    {"symbol": "BRIGADE", "name": "Brigade Enterprises", "sector": "Real Estate"},
    # Defense / Aerospace
    {"symbol": "COCHINSHIP", "name": "Cochin Shipyard", "sector": "Defense"},
    {"symbol": "MAZAGON", "name": "Mazagon Dock Shipbuilders", "sector": "Defense"},
    {"symbol": "GRSE", "name": "Garden Reach Shipbuilders", "sector": "Defense"},
    {"symbol": "BEML", "name": "BEML", "sector": "Defense"},
    # New-age / Internet
    {"symbol": "NYKAA", "name": "Nykaa (FSN E-Commerce)", "sector": "Consumer"},
    {"symbol": "PAYTM", "name": "Paytm (One 97 Communications)", "sector": "Fintech"},
    {"symbol": "POLICYBZR", "name": "PB Fintech (PolicyBazaar)", "sector": "Fintech"},
    {"symbol": "DELHIVERY", "name": "Delhivery", "sector": "Logistics"},
    # Indices (for reference)
    {"symbol": "^NSEI", "name": "NIFTY 50 Index", "sector": "Index"},
    {"symbol": "^BSESN", "name": "BSE SENSEX Index", "sector": "Index"},
]

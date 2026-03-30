from __future__ import annotations
"""
TradingView Screener integration for pre-screening Indian stocks.

Uses the `tradingview-screener` package (v3.1.0) to:
1. Fetch ALL NSE/BSE stocks (replacing the hardcoded 160 list).
2. Pre-screen stocks with momentum/volume/price filters before heavy yfinance calls.
3. Run generic screener queries for the UI.

Fallback: if TradingView screener is unavailable, falls back to MAJOR_STOCKS.
"""
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level cache for all Indian stocks (24-hour TTL, no Redis needed)
# ---------------------------------------------------------------------------
_all_stocks_cache: list[dict[str, Any]] = []
_all_stocks_cache_ts: float = 0.0
_CACHE_TTL_SECONDS: int = 24 * 60 * 60  # 24 hours

# TradingView columns we request for every query
_BASE_COLUMNS = [
    "name",
    "close",
    "volume",
    "change",
    "RSI",
    "Recommend.All",
    "average_volume_10d_calc",
    "average_volume_30d_calc",
    "market_cap_basic",
    "sector",
    "type",
]


def _is_cache_valid() -> bool:
    return bool(_all_stocks_cache) and (time.time() - _all_stocks_cache_ts) < _CACHE_TTL_SECONDS


def _parse_symbol_from_ticker(ticker: str) -> str:
    """
    TradingView returns tickers like 'NSE:RELIANCE' or 'BSE:TCS'.
    Strip the exchange prefix and return the raw symbol.
    """
    if ":" in ticker:
        return ticker.split(":", 1)[1]
    return ticker


def _safe_float(val: Any) -> Optional[float]:
    """Convert a value to float, returning None on failure."""
    if val is None:
        return None
    try:
        f = float(val)
        # pandas NaN check
        if f != f:
            return None
        return f
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# 1. get_all_indian_stocks — full NSE/BSE universe
# ---------------------------------------------------------------------------
def get_all_indian_stocks(force_refresh: bool = False) -> list[dict[str, Any]]:
    """
    Fetch ALL NSE/BSE stocks from TradingView screener.
    Returns list of dicts with symbol, name, exchange, sector, market_cap,
    close, volume, change_pct.

    Cached in module-level variable for 24 hours.
    Falls back to empty list on failure (caller should use MAJOR_STOCKS).
    """
    global _all_stocks_cache, _all_stocks_cache_ts

    if not force_refresh and _is_cache_valid():
        return _all_stocks_cache

    try:
        from tradingview_screener import Query

        count, df = (
            Query()
            .set_markets("india")
            .select(*_BASE_COLUMNS)
            .limit(5000)
            .get_scanner_data()
        )

        if df is None or df.empty:
            logger.warning("TradingView screener returned empty data for Indian stocks")
            return _all_stocks_cache  # return stale cache if available

        stocks: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            ticker_raw = str(row.get("ticker", ""))
            symbol = _parse_symbol_from_ticker(ticker_raw)
            exchange = ticker_raw.split(":")[0] if ":" in ticker_raw else "NSE"

            stocks.append({
                "symbol": symbol,
                "name": str(row.get("name", symbol)),
                "exchange": exchange,
                "sector": str(row.get("sector", "")) if row.get("sector") else "",
                "market_cap": _safe_float(row.get("market_cap_basic")),
                "close": _safe_float(row.get("close")),
                "volume": _safe_float(row.get("volume")),
                "change_pct": _safe_float(row.get("change")),
            })

        _all_stocks_cache = stocks
        _all_stocks_cache_ts = time.time()
        logger.info(f"TradingView screener: cached {len(stocks)} Indian stocks")
        return stocks

    except Exception as e:
        logger.error(f"TradingView screener fetch failed: {e}")
        return _all_stocks_cache  # return stale cache if available


# ---------------------------------------------------------------------------
# 2. pre_screen_stocks — filter before heavy yfinance scan
# ---------------------------------------------------------------------------
def pre_screen_stocks(
    filters: Optional[dict[str, Any]] = None,
) -> list[str]:
    """
    Pre-filter Indian stocks using TradingView built-in fields.
    Default filters (if none provided):
      - RSI < 30 OR RSI > 70 (momentum extremes)
      - volume > average_volume * 2 (volume spikes)
      - change > 3 OR change < -3 (price spikes)

    Returns a list of symbols matching ANY filter.
    Falls back to empty list on failure.
    """
    try:
        from tradingview_screener import Query, Column

        symbols: set[str] = set()

        # Limit per query — keep total candidates manageable for yfinance
        _LIMIT = 25

        # --- RSI extremes ---
        try:
            _count, df_oversold = (
                Query()
                .set_markets("india")
                .select("name", "close", "RSI", "volume", "change", "type", "market_cap_basic")
                .where(Column("RSI") < 30, Column("type") == "stock", Column("market_cap_basic") > 1e9)
                .order_by("market_cap_basic", ascending=False)
                .limit(_LIMIT)
                .get_scanner_data()
            )
            if df_oversold is not None and not df_oversold.empty:
                for _, row in df_oversold.iterrows():
                    symbols.add(_parse_symbol_from_ticker(str(row.get("ticker", ""))))
        except Exception as e:
            logger.warning(f"Screener RSI<30 query failed: {e}")

        try:
            _count, df_overbought = (
                Query()
                .set_markets("india")
                .select("name", "close", "RSI", "volume", "change", "type", "market_cap_basic")
                .where(Column("RSI") > 70, Column("type") == "stock", Column("market_cap_basic") > 1e9)
                .order_by("market_cap_basic", ascending=False)
                .limit(_LIMIT)
                .get_scanner_data()
            )
            if df_overbought is not None and not df_overbought.empty:
                for _, row in df_overbought.iterrows():
                    symbols.add(_parse_symbol_from_ticker(str(row.get("ticker", ""))))
        except Exception as e:
            logger.warning(f"Screener RSI>70 query failed: {e}")

        # --- Volume spikes (volume > 5M — proxy for high-volume days on large caps) ---
        # NOTE: Column * int not supported by tradingview_screener, so we use an
        # absolute volume threshold + large-cap filter instead of relative ratio.
        try:
            _count, df_vol = (
                Query()
                .set_markets("india")
                .select("name", "close", "volume", "change", "type", "market_cap_basic")
                .where(
                    Column("volume") > 5_000_000,
                    Column("type") == "stock",
                    Column("market_cap_basic") > 1e9,
                )
                .order_by("volume", ascending=False)
                .limit(_LIMIT)
                .get_scanner_data()
            )
            if df_vol is not None and not df_vol.empty:
                for _, row in df_vol.iterrows():
                    symbols.add(_parse_symbol_from_ticker(str(row.get("ticker", ""))))
        except Exception as e:
            logger.warning(f"Screener volume spike query failed: {e}")

        # --- Price spikes (change > 4% or < -4%, tighter than before to reduce noise) ---
        try:
            _count, df_up = (
                Query()
                .set_markets("india")
                .select("name", "close", "change", "type", "market_cap_basic")
                .where(Column("change") > 4, Column("type") == "stock", Column("market_cap_basic") > 1e9)
                .order_by("change", ascending=False)
                .limit(_LIMIT)
                .get_scanner_data()
            )
            if df_up is not None and not df_up.empty:
                for _, row in df_up.iterrows():
                    symbols.add(_parse_symbol_from_ticker(str(row.get("ticker", ""))))
        except Exception as e:
            logger.warning(f"Screener change>4 query failed: {e}")

        try:
            _count, df_down = (
                Query()
                .set_markets("india")
                .select("name", "close", "change", "type", "market_cap_basic")
                .where(Column("change") < -4, Column("type") == "stock", Column("market_cap_basic") > 1e9)
                .order_by("change", ascending=True)
                .limit(_LIMIT)
                .get_scanner_data()
            )
            if df_down is not None and not df_down.empty:
                for _, row in df_down.iterrows():
                    symbols.add(_parse_symbol_from_ticker(str(row.get("ticker", ""))))
        except Exception as e:
            logger.warning(f"Screener change<-4 query failed: {e}")

        # Remove empty strings and known-bad patterns
        symbols.discard("")
        # Filter out symbols that are unlikely to work with yfinance:
        # - Must be uppercase alphanumeric (with optional & and -)
        # - Skip very short symbols (< 2 chars) — often index artifacts
        # - Skip symbols with digits only (e.g., BSE numeric codes)
        valid_symbols = set()
        for sym in symbols:
            sym = sym.strip()
            if len(sym) < 2:
                continue
            if sym.isdigit():
                continue
            # Only keep symbols that match NSE equity naming convention
            import re
            if re.match(r'^[A-Z][A-Z0-9&\-]{0,19}$', sym):
                valid_symbols.add(sym)

        logger.info(
            "Pre-screen: %d stocks matched filters (%d after validation)",
            len(symbols), len(valid_symbols),
        )
        return list(valid_symbols)

    except Exception as e:
        logger.error(f"TradingView pre-screen failed entirely: {e}")
        return []


# ---------------------------------------------------------------------------
# 3. run_screener_query — generic screener for the UI
# ---------------------------------------------------------------------------
def run_screener_query(
    query_params: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Generic screener for the UI. Accepts filters:
      - rsi_min, rsi_max
      - volume_ratio_min (current vol / avg vol)
      - change_pct_min, change_pct_max
      - market_cap_min, market_cap_max
      - sector (optional string filter)
      - limit (default 50, max 200)

    Returns matching stocks with basic data.
    Falls back to empty list on failure.
    """
    try:
        from tradingview_screener import Query, Column

        limit = min(int(query_params.get("limit", 50)), 200)

        query = (
            Query()
            .set_markets("india")
            .select(
                "name", "close", "volume", "change", "RSI",
                "Recommend.All", "average_volume_10d_calc",
                "market_cap_basic", "sector",
                "MACD.macd", "MACD.signal", "Stoch.K", "Stoch.D", "ADX",
                "dividend_yield_recent",
            )
        )

        # Build filter conditions
        conditions = []

        rsi_min = query_params.get("rsi_min")
        rsi_max = query_params.get("rsi_max")
        if rsi_min is not None:
            conditions.append(Column("RSI") >= float(rsi_min))
        if rsi_max is not None:
            conditions.append(Column("RSI") <= float(rsi_max))

        volume_ratio_min = query_params.get("volume_ratio_min")
        if volume_ratio_min is not None:
            conditions.append(
                Column("volume") > Column("average_volume_10d_calc") * float(volume_ratio_min)
            )

        change_pct_min = query_params.get("change_pct_min")
        change_pct_max = query_params.get("change_pct_max")
        if change_pct_min is not None:
            conditions.append(Column("change") >= float(change_pct_min))
        if change_pct_max is not None:
            conditions.append(Column("change") <= float(change_pct_max))

        market_cap_min = query_params.get("market_cap_min")
        market_cap_max = query_params.get("market_cap_max")
        if market_cap_min is not None:
            conditions.append(Column("market_cap_basic") >= float(market_cap_min))
        if market_cap_max is not None:
            conditions.append(Column("market_cap_basic") <= float(market_cap_max))

        dividend_yield_min = query_params.get("dividend_yield_min")
        if dividend_yield_min is not None:
            conditions.append(Column("dividend_yield_recent") >= float(dividend_yield_min))

        sector = query_params.get("sector")
        if sector:
            conditions.append(Column("sector") == str(sector))

        # Apply all conditions
        for condition in conditions:
            query = query.where(condition)

        query = query.limit(limit)

        count, df = query.get_scanner_data()

        if df is None or df.empty:
            return []

        results: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            ticker_raw = str(row.get("ticker", ""))
            symbol = _parse_symbol_from_ticker(ticker_raw)
            exchange = ticker_raw.split(":")[0] if ":" in ticker_raw else "NSE"

            avg_vol = _safe_float(row.get("average_volume_10d_calc"))
            vol = _safe_float(row.get("volume"))
            volume_ratio = round(vol / avg_vol, 2) if vol and avg_vol and avg_vol > 0 else None

            results.append({
                "symbol": symbol,
                "name": str(row.get("name", symbol)),
                "exchange": exchange,
                "sector": str(row.get("sector", "")) if row.get("sector") else "",
                "close": _safe_float(row.get("close")),
                "change_pct": _safe_float(row.get("change")),
                "volume": vol,
                "volume_ratio": volume_ratio,
                "rsi": _safe_float(row.get("RSI")),
                "market_cap": _safe_float(row.get("market_cap_basic")),
                "recommendation": _safe_float(row.get("Recommend.All")),
                "macd": _safe_float(row.get("MACD.macd")),
                "macd_signal": _safe_float(row.get("MACD.signal")),
                "stoch_k": _safe_float(row.get("Stoch.K")),
                "stoch_d": _safe_float(row.get("Stoch.D")),
                "adx": _safe_float(row.get("ADX")),
                "dividend_yield": _safe_float(row.get("dividend_yield_recent")),
            })

        logger.info(f"Screener query returned {len(results)} results (total matched: {count})")
        return results

    except Exception as e:
        logger.error(f"TradingView screener query failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Preset screener configurations
# ---------------------------------------------------------------------------
SCREENER_PRESETS: dict[str, dict[str, Any]] = {
    "oversold": {
        "label": "Oversold (RSI < 30)",
        "description": "Stocks with RSI below 30, potentially due for a bounce.",
        "params": {"rsi_max": 30, "limit": 50},
    },
    "overbought": {
        "label": "Overbought (RSI > 70)",
        "description": "Stocks with RSI above 70, potentially overextended.",
        "params": {"rsi_min": 70, "limit": 50},
    },
    "volume_breakout": {
        "label": "Volume Breakout (2x avg)",
        "description": "Stocks trading at 2x+ their 10-day average volume.",
        "params": {"volume_ratio_min": 2, "limit": 50},
    },
    "momentum": {
        "label": "Strong Momentum (up >3%)",
        "description": "Stocks up more than 3% today with strong momentum.",
        "params": {"change_pct_min": 3, "limit": 50},
    },
    "large_cap_oversold": {
        "label": "Large Cap Oversold",
        "description": "Large cap stocks (>10K Cr) with RSI below 35.",
        "params": {"rsi_max": 35, "market_cap_min": 100_000_000_000, "limit": 50},
    },
    "sell_off": {
        "label": "Sell-off (down >3%)",
        "description": "Stocks down more than 3% today — potential capitulation or bad news.",
        "params": {"change_pct_max": -3, "limit": 50},
    },
    "dividend": {
        "label": "High Dividend Yield (>2%)",
        "description": "Stocks with dividend yield above 2% — income-generating picks.",
        "params": {"dividend_yield_min": 2, "limit": 50},
    },
}

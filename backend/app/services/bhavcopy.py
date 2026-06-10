from __future__ import annotations
"""
Bulk NSE EOD source — the whole-market "secondary market" bhavcopy.

One download (``sec_bhavdata_full_<DDMMYYYY>.csv``) carries OHLCV **and**
delivery % for every NSE equity for a trading day. That replaces the
per-symbol live-quote loop that trips NSE's anti-bot 403 wall: 1 archive
request serves the entire scan instead of ~50 hammering calls.

It is a *last-resort fallback* in ``data_fetcher`` — used for last-close
quotes and delivery % when the live sources (Upstox / NSE / yfinance) are all
parked. EOD only: it does not serve intraday or multi-day history.

The archive host (``nsearchives``) is far less protected than the public API,
but everything fails closed — any error returns ``None`` so callers cascade.
"""
import asyncio
import csv
import io
import logging
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ARCHIVE_URL = (
    "https://nsearchives.nseindia.com/products/content/"
    "sec_bhavdata_full_{ddmmyyyy}.csv"
)
_HTTP_TIMEOUT = 15.0
_CACHE_DIR = Path("/tmp/agentx_bhavcopy")
# The "latest available trading day" can shift once per day; re-resolve hourly.
_LATEST_TTL = 3600.0
# Equity series we keep (rolling-settlement equities + trade-for-trade).
_KEEP_SERIES = {"EQ", "BE", "BZ"}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/csv,*/*",
    "Referer": "https://www.nseindia.com/all-reports",
}

# In-memory cache of the resolved "latest" map: {trade_date_iso: {SYMBOL: row}}.
_latest_map: dict[str, dict[str, dict]] = {}
_latest_date_iso: Optional[str] = None
_latest_resolved_at: float = 0.0


def _f(val: Any) -> Optional[float]:
    """Parse a bhavcopy numeric cell — handles commas, blanks and '-'."""
    if val is None:
        return None
    s = str(val).strip().replace(",", "")
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_sec_bhavdata(text: str) -> dict[str, dict]:
    """Parse a ``sec_bhavdata_full`` CSV into ``{SYMBOL: normalized_row}``.

    Pure function — no I/O. NSE ships this file with a leading space in every
    header and cell (``" OPEN_PRICE"``); ``csv`` keeps those, so we strip both
    keys and values. Only equity series in ``_KEEP_SERIES`` are kept.
    """
    out: dict[str, dict] = {}
    reader = csv.DictReader(io.StringIO(text))
    for raw in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        series = row.get("SERIES", "")
        if series and series not in _KEEP_SERIES:
            continue
        symbol = row.get("SYMBOL", "")
        if not symbol:
            continue
        close = _f(row.get("CLOSE_PRICE"))
        if close is None:
            continue
        traded = _f(row.get("TTL_TRD_QNTY"))
        deliv_qty = _f(row.get("DELIV_QTY"))
        out[symbol.upper()] = {
            "symbol": symbol.upper(),
            "open": _f(row.get("OPEN_PRICE")),
            "high": _f(row.get("HIGH_PRICE")),
            "low": _f(row.get("LOW_PRICE")),
            "close": close,
            "last": _f(row.get("LAST_PRICE")),
            "prev_close": _f(row.get("PREV_CLOSE")),
            "volume": int(traded) if traded is not None else None,
            "delivery_qty": int(deliv_qty) if deliv_qty is not None else None,
            "delivery_pct": _f(row.get("DELIV_PER")),
        }
    return out


def _download_sec_bhavdata(trade_date: date) -> Optional[str]:
    """Download one day's bhavcopy CSV text. ``None`` on any failure (incl. the
    404 you get for weekends/holidays — the caller walks back a day)."""
    import requests

    ddmmyyyy = trade_date.strftime("%d%m%Y")
    url = _ARCHIVE_URL.format(ddmmyyyy=ddmmyyyy)
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_HTTP_TIMEOUT)
        if resp.status_code != 200 or not resp.text:
            logger.debug("bhavcopy %s: HTTP %s", ddmmyyyy, resp.status_code)
            return None
        return resp.text
    except Exception as e:
        logger.debug("bhavcopy %s download failed: %s", ddmmyyyy, e)
        return None


def _load_day(trade_date: date) -> Optional[dict[str, dict]]:
    """Return the parsed map for ``trade_date`` (disk cache → network)."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    disk = _CACHE_DIR / f"sec_bhavdata_full_{trade_date.strftime('%d%m%Y')}.csv"
    try:
        if disk.exists() and disk.stat().st_size > 0:
            return _parse_sec_bhavdata(disk.read_text())
    except Exception as e:  # pragma: no cover - disk corruption is rare
        logger.debug("bhavcopy cache unreadable for %s: %s", trade_date, e)

    text = _download_sec_bhavdata(trade_date)
    if not text:
        return None
    parsed = _parse_sec_bhavdata(text)
    if not parsed:
        return None
    try:
        disk.write_text(text)
    except Exception:  # pragma: no cover - cache write is best-effort
        pass
    return parsed


def _sync_get_latest(today: date, max_lookback: int = 6) -> Optional[str]:
    """Resolve and cache the most recent available bhavcopy at or before
    ``today``. Returns the resolved trade-date ISO string, or ``None``.

    Walks back day-by-day (markets are shut on weekends/holidays) up to
    ``max_lookback`` days. The resolved map is memoised for ``_LATEST_TTL``.
    """
    global _latest_date_iso, _latest_resolved_at
    now = time.monotonic()
    if _latest_date_iso and (now - _latest_resolved_at) < _LATEST_TTL:
        return _latest_date_iso

    for back in range(max_lookback):
        day = today - timedelta(days=back)
        if day.weekday() >= 5:  # Sat/Sun — no bhavcopy
            continue
        parsed = _load_day(day)
        if parsed:
            iso = day.isoformat()
            _latest_map[iso] = parsed
            _latest_date_iso = iso
            _latest_resolved_at = now
            logger.info("bhavcopy: loaded %d symbols for %s", len(parsed), iso)
            return iso
    logger.warning("bhavcopy: no file found in last %d days", max_lookback)
    return None


async def get_bhavcopy(trade_date: Optional[date] = None) -> dict[str, dict]:
    """Return ``{SYMBOL: row}`` for the latest available trading day (or a
    specific ``trade_date``). Empty dict on failure."""
    loop = asyncio.get_event_loop()
    if trade_date is not None:
        parsed = await loop.run_in_executor(None, _load_day, trade_date)
        return parsed or {}
    iso = await loop.run_in_executor(None, _sync_get_latest, date.today())
    return _latest_map.get(iso, {}) if iso else {}


async def get_eod_quote(symbol: str) -> Optional[dict[str, Any]]:
    """Quote-shaped last-close row for ``symbol`` from the bulk bhavcopy.

    Matches the ``get_stock_quote`` output shape so it drops into the waterfall
    as a final fallback. ``None`` if the symbol isn't in the latest file.
    """
    clean = symbol.replace(".NS", "").replace(".BO", "").upper()
    rows = await get_bhavcopy()
    row = rows.get(clean)
    if not row or row.get("close") is None:
        return None
    close = row["close"]
    prev = row.get("prev_close")
    change = round(close - prev, 2) if prev is not None else None
    pchange = round((close - prev) / prev * 100, 2) if prev else None
    return {
        "symbol": symbol,
        "lastPrice": close,
        "change": change,
        "pChange": pchange,
        "open": row.get("open"),
        "high": row.get("high"),
        "low": row.get("low"),
        "previousClose": prev,
        "totalTradedVolume": row.get("volume"),
        "source": "bhavcopy",
    }


async def get_delivery_pct(symbol: str) -> Optional[dict[str, Any]]:
    """Delivery-shaped row for ``symbol`` from the bulk bhavcopy.

    Matches the ``get_delivery_volume`` output shape. ``None`` if absent or the
    file carries no delivery figure for the symbol.
    """
    clean = symbol.replace(".NS", "").replace(".BO", "").upper()
    rows = await get_bhavcopy()
    row = rows.get(clean)
    if not row or row.get("delivery_pct") is None:
        return None
    return {
        "symbol": symbol,
        "delivery_pct": row["delivery_pct"],
        "traded_qty": row.get("volume"),
        "delivered_qty": row.get("delivery_qty"),
        "source": "bhavcopy",
    }

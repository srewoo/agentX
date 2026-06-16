from __future__ import annotations
"""Point-in-time (PIT) fundamentals — fundamentals *as they were known* on a date.

The single biggest, most-acknowledged bias in the quality/value backtester is
that yfinance returns **restated current** fundamentals and applies them to
*every* historical bar (quality_value_backtester.py header). That lets the
backtest "know" a 2024 ROE while trading in 2021 — look-ahead that inflates the
win rate by an estimated 2-4pp.

This module removes that look-ahead by sourcing **historical financial
statements tagged with their filing date** from FMP (which your repo already
keys for the earnings calendar) and exposing an as-of lookup:

    get_fundamentals_asof(symbol, "2021-06-30")
        → the most recent statement that was *already public* on 2021-06-30,
          i.e. filed on or before 2021-06-30 minus a small reporting lag.

A figure is never visible before the market could have seen it. The selection
core (:func:`select_asof`) is a pure function and is the correctness-critical
piece — it is unit-tested independently of the network.

Everything is **best-effort and graceful**: no FMP key, a restricted plan, or
a symbol FMP doesn't cover all return ``None``, and the caller falls back to
the legacy snapshot path (with the bias flag still raised in the backtest
output). Mirrors the key-handling / source_health / executor patterns in
``fmp_fetcher.py``.
"""
import asyncio
import logging
import time
from datetime import date, datetime, timedelta
from typing import Any, Optional

from app.services import source_health

logger = logging.getLogger(__name__)

SOURCE = "fmp"  # share the cooldown bucket with fmp_fetcher
_BASE = "https://financialmodelingprep.com/api/v3"
_TIMEOUT = 15.0

# Default reporting lag: a statement's `fillingDate` is when it hit the
# regulator, but markets need a beat to digest it. We additionally refuse to
# use a figure until `lag_days` after its filing — conservative, removes any
# same-day look-ahead.
DEFAULT_LAG_DAYS = 1

# PIT statement history is stable within a session — cache the parsed,
# filing-date-sorted statement list per symbol for the run.
_PIT_TTL = 6 * 3600.0
_pit_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _norm(symbol: str) -> str:
    """FMP keys Indian names with the `.NS` suffix; normalise to that."""
    s = (symbol or "").upper().strip()
    for suffix in (".NSE", ".BSE", ".BO"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    if not s.endswith(".NS"):
        s = f"{s}.NS"
    return s


def _to_date(v: Any) -> Optional[date]:
    """Parse an FMP date-ish value (YYYY-MM-DD or full timestamp) to a date."""
    if not v:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s:
        return None
    # FMP fillingDate is "YYYY-MM-DD"; acceptedDate may carry a time.
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[: len(fmt) + 2] if " " in s else s, fmt).date()
        except Exception:
            continue
    try:
        return datetime.fromisoformat(s).date()
    except Exception:
        return None


def _filing_date(row: dict[str, Any]) -> Optional[date]:
    """The date a statement became public.

    Prefer ``fillingDate`` (regulator filing). Fall back to ``acceptedDate``,
    then the period ``date`` — but a period-end date is a weak proxy and only
    used when nothing better exists.
    """
    return (
        _to_date(row.get("fillingDate"))
        or _to_date(row.get("acceptedDate"))
        or _to_date(row.get("date"))
    )


def select_asof(
    rows: list[dict[str, Any]],
    asof: date,
    lag_days: int = DEFAULT_LAG_DAYS,
) -> Optional[dict[str, Any]]:
    """Return the most recent statement public on/before ``asof`` (pure).

    "Public" means ``filing_date <= asof - lag_days``. This is the whole point
    of the module: a statement filed *after* the as-of date — or on the same
    day, within the lag — is invisible, exactly as it would have been to a
    trader standing on ``asof``. Returns ``None`` when no statement was yet
    public (e.g. backtesting a date before the company's first filing).

    ``rows`` may be in any order; selection is by filing date, not list order.
    """
    cutoff = asof - timedelta(days=max(0, lag_days))
    visible = [
        (fd, r)
        for r in rows
        if (fd := _filing_date(r)) is not None and fd <= cutoff
    ]
    if not visible:
        return None
    # Most recent filing that was already public.
    visible.sort(key=lambda t: t[0])
    return visible[-1][1]


def _fetch_statements_sync(symbol: str, api_key: str, limit: int) -> Optional[list[dict[str, Any]]]:
    """Fetch quarterly income + balance + ratios and merge by period date.

    Each merged row carries the period ``date`` plus the ``fillingDate`` /
    ``acceptedDate`` from the income statement (the authoritative filing).
    Returns ``None`` on any failure or a non-list payload (FMP returns an
    ``{"Error Message": ...}`` dict for a bad/over-limit/plan-restricted key).
    """
    import requests

    def _get(path: str) -> Optional[list[dict[str, Any]]]:
        try:
            resp = requests.get(
                f"{_BASE}/{path}/{symbol}",
                params={"period": "quarter", "limit": limit, "apikey": api_key},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data
            logger.debug("fmp_pit %s unexpected payload: %s", path, str(data)[:160])
            return None
        except Exception as e:
            logger.debug("fmp_pit %s fetch failed for %s: %s", path, symbol, e)
            return None

    income = _get("income-statement")
    if not income:
        return None
    balance = {r.get("date"): r for r in (_get("balance-sheet-statement") or [])}
    ratios = {r.get("date"): r for r in (_get("ratios") or [])}

    merged: list[dict[str, Any]] = []
    for inc in income:
        d = inc.get("date")
        bs = balance.get(d, {})
        rt = ratios.get(d, {})
        merged.append({
            "date": d,
            "fillingDate": inc.get("fillingDate"),
            "acceptedDate": inc.get("acceptedDate"),
            # Raw lines we derive metrics from (best-effort; absent → None).
            "netIncome": inc.get("netIncome"),
            "revenue": inc.get("revenue"),
            "ebitda": inc.get("ebitda"),
            "operatingCashFlow": rt.get("operatingCashFlowPerShare"),
            # Pre-computed ratios from FMP's /ratios (already PIT for the period).
            "peRatio": rt.get("priceEarningsRatio"),
            "roe": rt.get("returnOnEquity"),
            "debtToEquity": rt.get("debtEquityRatio"),
            "netDebtToEbitda": rt.get("netDebtToEBITDA"),
            "freeCashFlowPerShare": rt.get("freeCashFlowPerShare"),
        })
    return merged or None


async def _get_api_key() -> Optional[str]:
    try:
        from app.services.orchestrator import _get_settings
        settings = await _get_settings()
        return settings.get("fmp_api_key") or None
    except Exception as e:
        logger.debug("fmp_pit: settings load failed: %s", e)
        return None


async def get_pit_history(symbol: str, limit: int = 40) -> Optional[list[dict[str, Any]]]:
    """All available quarterly statements for ``symbol``, filing-date tagged.

    Cached per symbol for the session. ``None`` when FMP is unavailable
    (no key, cooldown, plan restriction, or uncovered symbol).
    """
    key_sym = _norm(symbol)
    cached = _pit_cache.get(key_sym)
    if cached and (time.time() - cached[0]) < _PIT_TTL:
        return cached[1]

    if source_health.is_down(SOURCE):
        return None
    api_key = await _get_api_key()
    if not api_key:
        return None

    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, _fetch_statements_sync, key_sym, api_key, limit)
    if rows is None:
        source_health.mark_down(SOURCE)
        return None
    source_health.mark_up(SOURCE)
    _pit_cache[key_sym] = (time.time(), rows)
    return rows


def _normalise(row: dict[str, Any]) -> dict[str, Any]:
    """Map a raw merged statement row to the metric keys callers expect."""
    def _f(v: Any) -> Optional[float]:
        try:
            return float(v) if v is not None else None
        except Exception:
            return None
    roe = _f(row.get("roe"))
    # FMP returns ROE as a fraction (0.18); callers/scorers expect percent.
    if roe is not None and abs(roe) <= 5:
        roe *= 100.0
    return {
        "pe": _f(row.get("peRatio")),
        "roe": roe,
        "net_debt_to_ebitda": _f(row.get("netDebtToEbitda")),
        "fcf_per_share": _f(row.get("freeCashFlowPerShare")),
        "debt_to_equity": _f(row.get("debtToEquity")),
        "as_of_period": row.get("date"),
        "filing_date": row.get("fillingDate") or row.get("acceptedDate"),
    }


async def get_fundamentals_asof(
    symbol: str,
    asof: date | str,
    *,
    lag_days: int = DEFAULT_LAG_DAYS,
) -> Optional[dict[str, Any]]:
    """Fundamentals for ``symbol`` *as public on* ``asof``.

    Returns a normalised dict ``{pe, roe, net_debt_to_ebitda, fcf_per_share,
    debt_to_equity, as_of_period, filing_date}`` from the most recent
    statement filed on/before ``asof - lag_days``, or ``None`` when PIT data
    is unavailable or nothing was public yet on that date.
    """
    asof_d = _to_date(asof)
    if asof_d is None:
        return None
    history = await get_pit_history(symbol)
    if not history:
        return None
    row = select_asof(history, asof_d, lag_days=lag_days)
    if row is None:
        return None
    return _normalise(row)


def _reset_cache() -> None:
    """Test helper — clear the PIT statement cache."""
    _pit_cache.clear()

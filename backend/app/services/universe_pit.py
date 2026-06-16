from __future__ import annotations
"""Point-in-time backtest universe — survivorship-aware constituent selection.

Every backtester on free data shares one bias: it scans **today's** index
members across historical periods, so companies delisted/de-indexed in the
window never appear and the win rate is inflated (~1-3pp, per the
quality_value_backtester header). Correcting it requires knowing *who was a
member on a given date*, including names that have since vanished — and there
is **no free source** of historical NSE constituents or delisted-name price
data (yfinance won't even return bars for a delisted ticker).

So this module does not pretend to solve survivorship — it provides the
**seam** that makes the fix a data-drop, not a code change:

  • If ``backend/models/nse_constituents_history.csv`` exists (columns
    ``symbol,added,removed`` — ISO dates, ``removed`` blank = still a member),
    :func:`get_universe_at_date` returns the members active on ``asof`` and
    reports ``survivorship_free=True``.
  • Otherwise it falls back to today's static ``MAJOR_STOCKS`` and reports
    ``survivorship_free=False`` so the backtest output flags the bias honestly.

Drop a real history CSV (from NSE archives or a vendor) and survivorship-free
backtests light up with zero code changes.
"""
import csv
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Optional data drop. Absent by default — see module docstring.
_HISTORY_CSV = Path(__file__).resolve().parent.parent.parent / "models" / "nse_constituents_history.csv"


def _to_date(v: object) -> Optional[date]:
    if not v:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        try:
            return datetime.fromisoformat(s).date()
        except Exception:
            return None


def has_constituent_history() -> bool:
    """True when a historical-constituents CSV is present to drive PIT universes."""
    return _HISTORY_CSV.exists()


def _load_history() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    try:
        with _HISTORY_CSV.open(newline="") as f:
            for r in csv.DictReader(f):
                sym = (r.get("symbol") or "").strip().upper()
                if not sym:
                    continue
                rows.append({
                    "symbol": sym,
                    "added": _to_date(r.get("added")),
                    "removed": _to_date(r.get("removed")),
                })
    except Exception as e:
        logger.warning("universe_pit: failed to read %s: %s", _HISTORY_CSV, e)
    return rows


def members_at(rows: list[dict[str, object]], asof: date) -> list[str]:
    """Pure: symbols that were members on ``asof`` (added ≤ asof < removed).

    A blank/None ``removed`` means still a member. A blank ``added`` is treated
    as "member since before the dataset began" (always-on lower bound).
    """
    out: list[str] = []
    for r in rows:
        added = r.get("added")
        removed = r.get("removed")
        if added is not None and asof < added:  # type: ignore[operator]
            continue
        if removed is not None and asof >= removed:  # type: ignore[operator]
            continue
        out.append(str(r["symbol"]))
    # De-dupe, preserve order.
    seen: set[str] = set()
    return [s for s in out if not (s in seen or seen.add(s))]


def get_universe_at_date(
    asof: date | str,
    *,
    limit: Optional[int] = None,
) -> tuple[list[str], bool]:
    """Return ``(symbols, survivorship_free)`` for the universe as of ``asof``.

    With a constituents-history CSV present, returns the point-in-time members
    and ``survivorship_free=True``. Without it, returns today's static
    ``MAJOR_STOCKS`` and ``survivorship_free=False`` — the caller must surface
    that the result still carries survivorship bias.
    """
    asof_d = _to_date(asof)
    if has_constituent_history() and asof_d is not None:
        rows = _load_history()
        syms = members_at(rows, asof_d)
        if syms:
            return (syms[:limit] if limit else syms), True
        logger.warning("universe_pit: history CSV present but no members at %s — falling back", asof_d)

    # Fallback: today's static list. Survivorship bias remains.
    from app.services.data_fetcher import MAJOR_STOCKS
    syms = [s["symbol"] for s in MAJOR_STOCKS if not s["symbol"].startswith("^")]
    return (syms[:limit] if limit else syms), False

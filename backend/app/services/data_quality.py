from __future__ import annotations
"""3.3 — Cross-source reconciliation + persisted data-quality ledger.

Data bugs used to reach a backtest silently: a missed corporate action or a
source disagreeing by 3% would only ever surface as a one-off ``logger.warning``
nobody reads. This module turns those into a persisted, queryable ledger and a
nightly cross-source reconciliation so data quality is OBSERVABLE — surfaced at
``/api/health`` and alertable.

Two detectors:
  * **Suspicious jumps** — residual one-day close-to-close moves past a threshold
    (a missed split/bonus), extending ``price_adjuster.flag_suspicious_jumps``.
  * **Cross-source disagreement** — the same day's close from Upstox / NSE /
    yfinance must agree within ``DISAGREEMENT_PCT`` (0.5%); a wider spread means
    at least one source is wrong and any backtest reading it is contaminated.

Pure detectors (``detect_jumps``, ``compare_sources``) are separated from the DB
so they unit-test cleanly.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite

from app.database import DB_PATH

logger = logging.getLogger(__name__)

DISAGREEMENT_PCT = 0.5      # cross-source close disagreement that trips an alert
SUSPICIOUS_JUMP_PCT = 45.0  # residual one-day move that looks like a missed action

CREATE_DATA_QUALITY_TABLE = """
CREATE TABLE IF NOT EXISTS data_quality_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL,           -- 'suspicious_jump' | 'source_disagreement'
    detail TEXT,
    value REAL,                   -- the offending % (jump size or max spread)
    sources TEXT,                 -- JSON of source→value for disagreements
    detected_at TEXT NOT NULL
);
"""


# ── pure detectors ───────────────────────────────────────────
def detect_jumps(df, symbol: str, threshold_pct: float = SUSPICIOUS_JUMP_PCT) -> list[dict[str, Any]]:
    """Residual close-to-close jumps past ``threshold_pct`` (missed corp action)."""
    if df is None or len(df) < 2 or "Close" not in getattr(df, "columns", []):
        return []
    out: list[dict[str, Any]] = []
    try:
        rets = df["Close"].pct_change().abs() * 100.0
        for ts, pct in rets[rets > threshold_pct].items():
            out.append({"symbol": symbol, "kind": "suspicious_jump",
                        "value": round(float(pct), 3),
                        "detail": f"{float(pct):.1f}% close-to-close move on {ts}"})
    except Exception as e:
        logger.debug("detect_jumps failed for %s: %s", symbol, e)
    return out


def compare_sources(
    prices: dict[str, float], threshold_pct: float = DISAGREEMENT_PCT,
) -> Optional[dict[str, Any]]:
    """Return a disagreement record if the max pairwise close spread exceeds
    ``threshold_pct``, else None. Needs ≥2 positive prices."""
    valid = {s: float(p) for s, p in prices.items() if p and float(p) > 0}
    if len(valid) < 2:
        return None
    lo, hi = min(valid.values()), max(valid.values())
    spread_pct = (hi - lo) / lo * 100.0
    if spread_pct <= threshold_pct:
        return None
    return {"kind": "source_disagreement", "value": round(spread_pct, 3),
            "sources": valid,
            "detail": f"close spread {spread_pct:.2f}% across {', '.join(valid)}"}


# ── ledger (DB) ──────────────────────────────────────────────
async def _ensure(db: aiosqlite.Connection) -> None:
    await db.execute(CREATE_DATA_QUALITY_TABLE)


async def record_issues(issues: list[dict[str, Any]], *, db_path: Optional[str] = None) -> int:
    """Persist detected issues to the ledger. Returns the count written."""
    if not issues:
        return 0
    import json
    path = db_path or DB_PATH
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(path) as db:
        await _ensure(db)
        for i in issues:
            await db.execute(
                "INSERT INTO data_quality_issues (symbol, kind, detail, value, "
                "sources, detected_at) VALUES (?,?,?,?,?,?)",
                (i.get("symbol", "?"), i["kind"], i.get("detail"),
                 i.get("value"), json.dumps(i["sources"]) if i.get("sources") else None, now),
            )
        await db.commit()
    return len(issues)


async def recent_issues(*, limit: int = 20, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    path = db_path or DB_PATH
    try:
        async with aiosqlite.connect(path) as db:
            await _ensure(db)
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT symbol, kind, detail, value, detected_at "
                "FROM data_quality_issues ORDER BY id DESC LIMIT ?", (limit,)
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
    except Exception as e:
        logger.debug("recent_issues failed: %s", e)
        return []


async def health_summary(*, db_path: Optional[str] = None) -> dict[str, Any]:
    """Compact data-quality summary for /api/health: counts by kind + last seen."""
    path = db_path or DB_PATH
    try:
        async with aiosqlite.connect(path) as db:
            await _ensure(db)
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT kind, COUNT(*) n, MAX(detected_at) last FROM data_quality_issues "
                "GROUP BY kind"
            ) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
    except Exception as e:
        logger.debug("health_summary failed: %s", e)
        return {"status": "unknown", "issues": {}}
    by_kind = {r["kind"]: {"count": r["n"], "last": r["last"]} for r in rows}
    total = sum(v["count"] for v in by_kind.values())
    return {"status": "clean" if total == 0 else "issues_present",
            "total_issues": total, "by_kind": by_kind}


# ── reconciliation job ───────────────────────────────────────
async def _latest_close(symbol: str, source: str) -> Optional[float]:
    """Best-effort last close for ``symbol`` from a specific source."""
    try:
        if source == "yfinance":
            from app.services.data_fetcher import _yfinance_fetch_sync
            import asyncio
            df = await asyncio.get_event_loop().run_in_executor(
                None, _yfinance_fetch_sync, symbol, "5d", "1d", "NSE")
        elif source == "nse":
            from app.services.nse_fetcher import nse_fetch_history
            df = await nse_fetch_history(symbol, period="5d")
        elif source == "upstox":
            from app.services import upstox_fetcher
            df = await upstox_fetcher.fetch_history(symbol, period="5d", interval="1d")
        else:
            return None
        if df is not None and not df.empty and "Close" in df.columns:
            return float(df["Close"].iloc[-1])
    except Exception as e:
        logger.debug("close fetch %s/%s failed: %s", symbol, source, e)
    return None


async def run_reconciliation(
    symbols: list[str], *, sources: tuple[str, ...] = ("upstox", "nse", "yfinance"),
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Nightly job: compare last close across sources per symbol; record & count
    disagreements > DISAGREEMENT_PCT. Fail-open per symbol/source."""
    import asyncio
    issues: list[dict[str, Any]] = []
    jumps_found = 0
    for sym in symbols:
        prices = dict(zip(sources, await asyncio.gather(
            *(_latest_close(sym, s) for s in sources))))
        disagreement = compare_sources({s: p for s, p in prices.items() if p})
        if disagreement:
            disagreement["symbol"] = sym
            issues.append(disagreement)
            logger.warning("data-quality: %s close disagreement %.2f%% across sources",
                           sym, disagreement["value"])
        # Also scan the canonical (waterfall) frame for missed corporate actions.
        try:
            from app.services.data_fetcher import async_fetch_history
            df = await async_fetch_history(sym, period="1y", interval="1d")
            jumps = detect_jumps(df, sym)
            issues.extend(jumps)
            jumps_found += len(jumps)
        except Exception as e:
            logger.debug("jump scan failed for %s: %s", sym, e)
    written = await record_issues(issues, db_path=db_path)
    return {"symbols_checked": len(symbols), "disagreements": len(issues) - jumps_found,
            "suspicious_jumps": jumps_found, "recorded": written}

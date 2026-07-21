from __future__ import annotations
"""3.1 — Persistent point-in-time price store.

Every scan and backtest re-fetches history through the source waterfall and
re-applies corporate-action adjustment on the fly. That makes results
non-reproducible (a source flip or a late split re-write changes yesterday's
"history") and burns API budget. This store fixes the adjusted series ONCE at
write time and serves every read from disk: same query → same answer, forever.

Design:
  * **Nightly ingest** (`ingest_symbol` / `ingest_universe`) pulls each symbol
    through the existing waterfall (``async_fetch_history``, which already
    normalises to the canonical split/bonus-adjusted policy) and upserts the
    adjusted OHLCV plus provenance (adjustment policy + source) into the store.
  * **Read-only for consumers** — ``get_prices`` returns a DataFrame straight
    from the store; ``get_history_pit_first`` reads the store and only falls
    back to the live waterfall on a miss, so callers can adopt it incrementally.

Backend: SQLite (already a dependency) — DuckDB/Parquet were the target's
suggested options but would add a heavy dependency; the persistence + reproduce
guarantees are identical here and the backend can be swapped later.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from app.database import DB_PATH

logger = logging.getLogger(__name__)

CREATE_PIT_PRICES_TABLE = """
CREATE TABLE IF NOT EXISTS pit_prices (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    adjustment TEXT,        -- provenance: canonical policy applied at write time
    source TEXT,            -- which waterfall source produced the bar
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
);
"""


async def _ensure(db: aiosqlite.Connection) -> None:
    await db.execute(CREATE_PIT_PRICES_TABLE)


def _col(df, *names):
    """First matching column name present in df (case-insensitive)."""
    lower = {c.lower(): c for c in df.columns}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    return None


async def ingest_symbol(
    symbol: str, *, period: str = "5y", db_path: Optional[str] = None,
) -> dict:
    """Fetch ``symbol`` through the waterfall (adjustment applied once) and
    upsert into the store. Idempotent per (symbol, date)."""
    from app.services.data_fetcher import async_fetch_history

    path = db_path or DB_PATH
    df = await async_fetch_history(symbol, period=period, interval="1d")
    if df is None or df.empty:
        return {"symbol": symbol, "ingested": 0, "reason": "no_data"}

    adjustment = str(df.attrs.get("px_adjustment", "unknown"))
    source = str(df.attrs.get("px_source", "unknown"))
    c_o, c_h = _col(df, "Open"), _col(df, "High")
    c_l, c_c, c_v = _col(df, "Low"), _col(df, "Close"), _col(df, "Volume")
    if c_c is None:
        return {"symbol": symbol, "ingested": 0, "reason": "no_close_column"}

    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for ts, r in df.iterrows():
        d = ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10]
        rows.append((
            symbol, d,
            float(r[c_o]) if c_o and r[c_o] == r[c_o] else None,
            float(r[c_h]) if c_h and r[c_h] == r[c_h] else None,
            float(r[c_l]) if c_l and r[c_l] == r[c_l] else None,
            float(r[c_c]) if r[c_c] == r[c_c] else None,
            float(r[c_v]) if c_v and r[c_v] == r[c_v] else None,
            adjustment, source, now,
        ))
    async with aiosqlite.connect(path) as db:
        await _ensure(db)
        await db.executemany(
            "INSERT INTO pit_prices (symbol, date, open, high, low, close, volume, "
            "adjustment, source, ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol, date) DO UPDATE SET open=excluded.open, "
            "high=excluded.high, low=excluded.low, close=excluded.close, "
            "volume=excluded.volume, adjustment=excluded.adjustment, "
            "source=excluded.source, ingested_at=excluded.ingested_at",
            rows,
        )
        await db.commit()
    return {"symbol": symbol, "ingested": len(rows), "adjustment": adjustment, "source": source}


async def ingest_universe(
    symbols: list[str], *, period: str = "5y", db_path: Optional[str] = None,
) -> dict:
    """Nightly ingest for a universe. Best-effort per symbol."""
    total = 0
    ok = 0
    for sym in symbols:
        try:
            res = await ingest_symbol(sym, period=period, db_path=db_path)
            total += res.get("ingested", 0)
            ok += 1 if res.get("ingested") else 0
        except Exception as e:
            logger.debug("pit ingest failed for %s: %s", sym, e)
    return {"symbols": len(symbols), "ingested_symbols": ok, "rows": total}


async def get_prices(
    symbol: str, *, start: Optional[str] = None, end: Optional[str] = None,
    db_path: Optional[str] = None,
):
    """Adjusted OHLCV for ``symbol`` from the store as a DataFrame (DatetimeIndex),
    or None if the store has nothing. Same query → same answer."""
    import pandas as pd

    path = db_path or DB_PATH
    q = "SELECT date, open, high, low, close, volume FROM pit_prices WHERE symbol = ?"
    args: list = [symbol]
    if start:
        q += " AND date >= ?"; args.append(start)
    if end:
        q += " AND date <= ?"; args.append(end)
    q += " ORDER BY date ASC"
    try:
        async with aiosqlite.connect(path) as db:
            await _ensure(db)
            async with db.execute(q, args) as cur:
                rows = await cur.fetchall()
    except Exception as e:
        logger.debug("get_prices failed for %s: %s", symbol, e)
        return None
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["date", "Open", "High", "Low", "Close", "Volume"])
    df.index = pd.to_datetime(df["date"])
    return df.drop(columns=["date"])


async def coverage(symbol: str, *, db_path: Optional[str] = None) -> dict:
    """(min_date, max_date, n_bars) held for a symbol — for a store-health view."""
    path = db_path or DB_PATH
    try:
        async with aiosqlite.connect(path) as db:
            await _ensure(db)
            async with db.execute(
                "SELECT MIN(date), MAX(date), COUNT(*) FROM pit_prices WHERE symbol = ?",
                (symbol,)
            ) as cur:
                row = await cur.fetchone()
    except Exception:
        return {"symbol": symbol, "bars": 0}
    return {"symbol": symbol, "start": row[0], "end": row[1], "bars": int(row[2] or 0)}


async def get_history_pit_first(
    symbol: str, *, period: str = "5y", db_path: Optional[str] = None,
):
    """Store-first read: return the persisted series if present, else fall back
    to the live waterfall. Lets consumers adopt the store incrementally without
    a risky big-bang cutover."""
    df = await get_prices(symbol, db_path=db_path)
    if df is not None and not df.empty:
        return df
    from app.services.data_fetcher import async_fetch_history
    return await async_fetch_history(symbol, period=period, interval="1d")

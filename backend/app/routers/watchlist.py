"""Watchlist CRUD endpoints."""
import logging
from datetime import datetime, timezone

import aiosqlite
from fastapi import APIRouter, HTTPException

from app.database import DB_PATH
from app.models import AddWatchlistRequest
from app.utils import sanitize_symbol

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])
logger = logging.getLogger(__name__)


@router.get("")
async def get_watchlist():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM watchlist ORDER BY added_at DESC") as cursor:
            rows = await cursor.fetchall()
    return {"watchlist": [dict(r) for r in rows]}


@router.post("")
async def add_to_watchlist(body: AddWatchlistRequest):
    symbol = sanitize_symbol(body.symbol)
    if not symbol:
        raise HTTPException(status_code=400, detail="Invalid symbol")

    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO watchlist (symbol, name, exchange, added_at) VALUES (?, ?, ?, ?)",
                (symbol, body.name or symbol, body.exchange or "NSE", datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            raise HTTPException(status_code=409, detail=f"{symbol} is already in watchlist")

    return {"item": {"symbol": symbol, "name": body.name, "exchange": body.exchange}}


@router.delete("/{symbol}")
async def remove_from_watchlist(symbol: str):
    symbol = sanitize_symbol(symbol)
    async with aiosqlite.connect(DB_PATH) as db:
        result = await db.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))
        await db.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"{symbol} not found in watchlist")
    return {"ok": True}

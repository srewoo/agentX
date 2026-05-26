from __future__ import annotations
"""Broker setup + trade-journal endpoints.

* POST /api/broker/test — attempts to log in to the configured broker and
  fetch a quick reference quote. Returns a status payload the extension's
  Settings page can render as "Connected ✓".
* GET  /api/broker/status — last known connection state.
* GET  /api/broker/journal — paginated trade journal (paper + live).
"""
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.database import DB_PATH

router = APIRouter(prefix="/api/broker", tags=["broker"])
logger = logging.getLogger(__name__)


_JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broker TEXT NOT NULL,          -- 'paper' | 'kite' | 'angelone'
    mode TEXT NOT NULL,            -- 'dry_run' | 'live'
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    intended_price REAL,
    fill_price REAL,
    slippage_bps REAL,
    qty INTEGER,
    status TEXT NOT NULL,          -- 'placed' | 'filled' | 'rejected' | 'cancelled'
    signal_id TEXT,
    rec_id TEXT,
    notes TEXT,
    created_at TEXT NOT NULL
);
"""

_JOURNAL_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_trade_journal_created_at "
    "ON trade_journal(created_at DESC)"
)


async def _ensure_journal_schema() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(_JOURNAL_SCHEMA)
        await db.execute(_JOURNAL_INDEX)
        await db.commit()


async def _load_settings() -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT key, value FROM settings") as cur:
                async for r in cur:
                    out[r["key"]] = r["value"]
    except Exception:
        pass
    return out


@router.get("/status")
async def broker_status() -> dict[str, Any]:
    """Return what broker is currently selected and whether creds exist.

    Read-only — does NOT attempt a login. Use /test for that.
    """
    s = await _load_settings()
    selected = (s.get("broker_provider") or "paper").lower()
    creds_ok = False
    if selected == "kite":
        creds_ok = bool(s.get("kite_api_key") and s.get("kite_access_token"))
    elif selected == "angelone":
        creds_ok = bool(
            s.get("angelone_api_key") and s.get("angelone_client_id")
            and s.get("angelone_password") and s.get("angelone_totp_secret")
        )
    elif selected == "paper":
        creds_ok = True

    return {
        "broker": selected,
        "credentials_present": creds_ok,
        "last_check_iso": s.get("broker_last_check_iso"),
        "last_check_ok": s.get("broker_last_check_ok") == "1",
    }


class TestBrokerRequest(BaseModel):
    broker: Optional[str] = None
    probe_symbol: Optional[str] = "RELIANCE"


@router.post("/test")
async def test_broker(body: TestBrokerRequest) -> dict[str, Any]:
    """Attempt a live login + a probe quote. Returns the outcome.

    Persists `broker_last_check_iso` and `broker_last_check_ok` in
    settings so the popup can display state without re-running the probe.
    """
    s = await _load_settings()
    broker_name = (body.broker or s.get("broker_provider") or "paper").lower()

    if broker_name == "paper":
        await _record_check(broker_name, ok=True, note="paper broker — no creds required")
        return {"broker": "paper", "ok": True, "message": "Paper broker active"}

    try:
        from app.services.broker import get_broker_client
        # Force broker_provider for this evaluation.
        eff_settings = {**s, "broker_provider": broker_name}
        client = get_broker_client(eff_settings)
    except Exception as e:
        await _record_check(broker_name, ok=False, note=f"import error: {e}")
        raise HTTPException(status_code=500, detail=f"broker client unavailable: {e}")

    if client is None:
        msg = f"no client configured for {broker_name} (check credentials)"
        await _record_check(broker_name, ok=False, note=msg)
        return {"broker": broker_name, "ok": False, "message": msg}

    # Best-effort login.
    try:
        ok = await client.login()
    except Exception as e:
        await _record_check(broker_name, ok=False, note=f"login exception: {e}")
        return {"broker": broker_name, "ok": False, "message": f"login failed: {e}"}

    if not ok:
        await _record_check(broker_name, ok=False, note="login returned False")
        return {"broker": broker_name, "ok": False, "message": "login returned False"}

    # Probe one quote to confirm market-data scope.
    probe = body.probe_symbol or "RELIANCE"
    quote_ok = False
    quote_payload: dict[str, Any] | None = None
    try:
        q = await client.get_quote(probe)
        if q is not None:
            quote_ok = True
            quote_payload = {
                "symbol": getattr(q, "symbol", probe),
                "ltp": getattr(q, "ltp", None),
                "ts": getattr(q, "timestamp", None),
            }
    except Exception as e:
        logger.debug("broker probe quote failed: %s", e)

    await _record_check(
        broker_name,
        ok=quote_ok,
        note=f"probe={probe} quote_ok={quote_ok}",
    )

    return {
        "broker": broker_name,
        "ok": quote_ok,
        "message": "Connected" if quote_ok else "Login OK but quote probe failed",
        "probe": quote_payload,
    }


async def _record_check(broker: str, *, ok: bool, note: str) -> None:
    try:
        ts = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            for k, v in [
                ("broker_last_check_iso", ts),
                ("broker_last_check_ok", "1" if ok else "0"),
                ("broker_last_check_note", note),
                ("broker_last_check_provider", broker),
            ]:
                await db.execute(
                    "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                    (k, v),
                )
            await db.commit()
    except Exception as e:
        logger.debug("broker check persist failed: %s", e)


@router.get("/kite/login-url")
async def kite_login_url() -> dict[str, Any]:
    """Return the Zerodha Kite login URL the extension should open.

    Flow:
      1. Extension calls this endpoint → user is redirected to Kite login.
      2. Kite redirects back to our `kite_redirect_url` with `?request_token=...`.
      3. Extension POSTs the request_token to `/api/broker/kite/exchange-token`.
      4. Backend exchanges it for an `access_token`, persists it sealed.
    """
    s = await _load_settings()
    api_key = s.get("kite_api_key")
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="kite_api_key is not configured; set it in Settings first",
        )
    # Kite's official login URL format.
    url = f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"
    return {
        "login_url": url,
        "redirect_hint": s.get("kite_redirect_url"),
        "note": (
            "Open the login_url in the browser. After Zerodha redirects back, "
            "POST the request_token to /api/broker/kite/exchange-token."
        ),
    }


class KiteExchangeRequest(BaseModel):
    request_token: str


@router.post("/kite/exchange-token")
async def kite_exchange_token(body: KiteExchangeRequest) -> dict[str, Any]:
    """Exchange Kite `request_token` for an `access_token` and persist it."""
    s = await _load_settings()
    api_key = s.get("kite_api_key")
    api_secret = s.get("kite_api_secret")
    if not api_key or not api_secret:
        raise HTTPException(
            status_code=400,
            detail="kite_api_key and kite_api_secret must both be configured",
        )
    try:
        from kiteconnect import KiteConnect  # type: ignore
    except ImportError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Kite SDK not installed: {e}. pip install kiteconnect",
        )

    try:
        kite = KiteConnect(api_key=api_key)
        sess = kite.generate_session(body.request_token, api_secret=api_secret)
        access_token = sess.get("access_token")
        if not access_token:
            raise RuntimeError("response missing access_token")
    except Exception as e:
        await _record_check("kite", ok=False, note=f"exchange-token failed: {e}")
        raise HTTPException(status_code=502, detail=f"kite exchange failed: {e}")

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                ("kite_access_token", access_token),
            )
            # Auto-select Kite as broker provider on first successful exchange.
            if (s.get("broker_provider") or "paper") == "paper":
                await db.execute(
                    "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                    ("broker_provider", "kite"),
                )
            await db.commit()
    except Exception as e:
        logger.exception("kite token persist failed: %s", e)
        raise HTTPException(status_code=500, detail=f"persist failed: {e}")

    await _record_check("kite", ok=True, note="exchange-token succeeded")
    return {"ok": True, "broker": "kite", "message": "Access token saved"}


# In-memory subscription registry — keyed by symbol. The first subscriber
# starts the broker WS; the last unsubscribe stops it.
_WS_SUBS: dict[str, dict[str, Any]] = {}


@router.post("/ws/subscribe")
async def ws_subscribe(symbols: list[str]) -> dict[str, Any]:
    """Register the given symbols for live tick streaming.

    This is a thin control-plane endpoint — actual tick delivery happens
    via `/api/broker/ws/stream` (SSE). We register the subscription so a
    future broker_ws worker can pick it up. For now, the SSE stream
    emits cached quotes at 2s cadence as a stop-gap.
    """
    s = await _load_settings()
    for sym in symbols:
        sym_u = sym.upper()
        _WS_SUBS[sym_u] = {
            "subscribed_at": datetime.now(timezone.utc).isoformat(),
            "broker": (s.get("broker_provider") or "paper").lower(),
        }
    return {"ok": True, "subscribed": list(_WS_SUBS.keys())}


@router.post("/ws/unsubscribe")
async def ws_unsubscribe(symbols: list[str]) -> dict[str, Any]:
    for sym in symbols:
        _WS_SUBS.pop(sym.upper(), None)
    return {"ok": True, "remaining": list(_WS_SUBS.keys())}


@router.get("/ws/stream")
async def ws_stream() -> Any:
    """SSE tick feed for currently-subscribed symbols.

    True broker WS plumbing is a multi-day task — until then this emits
    cached quotes at 2s cadence via the existing data_fetcher cache, so
    the popup's Live tab has a real stream to bind against.
    """
    from fastapi.responses import StreamingResponse
    import asyncio

    async def gen():
        from app.services.data_fetcher import get_stock_quote
        while True:
            if not _WS_SUBS:
                yield "event: idle\ndata: {}\n\n"
            else:
                for sym in list(_WS_SUBS.keys()):
                    try:
                        q = await get_stock_quote(sym)
                    except Exception:
                        q = None
                    payload = {"symbol": sym, "quote": q, "ts": datetime.now(timezone.utc).isoformat()}
                    import json as _j
                    yield f"event: tick\ndata: {_j.dumps(payload, default=str)}\n\n"
            await asyncio.sleep(2.0)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/journal")
async def get_journal(
    limit: int = Query(default=100, ge=1, le=500),
    symbol: Optional[str] = None,
    mode: Optional[str] = Query(default=None, pattern="^(dry_run|live)$"),
) -> dict[str, Any]:
    """List trade-journal entries — paper and live combined."""
    await _ensure_journal_schema()
    where = ["1=1"]
    params: list[Any] = []
    if symbol:
        where.append("symbol = ?")
        params.append(symbol.upper())
    if mode:
        where.append("mode = ?")
        params.append(mode)
    params.append(limit)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM trade_journal WHERE {' AND '.join(where)} "
            f"ORDER BY id DESC LIMIT ?",
            params,
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return {"data": rows, "count": len(rows)}


class JournalAppendRequest(BaseModel):
    broker: str
    mode: str
    symbol: str
    direction: str
    intended_price: Optional[float] = None
    fill_price: Optional[float] = None
    qty: Optional[int] = None
    status: str = "placed"
    signal_id: Optional[str] = None
    rec_id: Optional[str] = None
    notes: Optional[str] = None


@router.post("/journal", status_code=201)
async def post_journal(body: JournalAppendRequest) -> dict[str, Any]:
    """Append a journal row. Computes slippage_bps when both prices set."""
    await _ensure_journal_schema()
    slippage_bps: Optional[float] = None
    if body.intended_price and body.fill_price and body.intended_price > 0:
        slippage_bps = round(
            abs(body.fill_price - body.intended_price)
            / body.intended_price * 10_000.0,
            2,
        )
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO trade_journal "
            " (broker, mode, symbol, direction, intended_price, fill_price, "
            "  slippage_bps, qty, status, signal_id, rec_id, notes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                body.broker, body.mode, body.symbol.upper(), body.direction,
                body.intended_price, body.fill_price, slippage_bps, body.qty,
                body.status, body.signal_id, body.rec_id, body.notes,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()
    return {"ok": True, "slippage_bps": slippage_bps}

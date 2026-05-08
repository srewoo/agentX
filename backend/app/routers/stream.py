"""Real-time price streaming WebSocket endpoint.

Contract:
- ``WS /api/stream/quotes?symbols=RELIANCE,TCS,INFY``
- Client may also send JSON:
  - ``{"type": "subscribe",   "symbols": [...]}``
  - ``{"type": "unsubscribe", "symbols": [...]}``
  - ``{"type": "pong"}``  (response to server heartbeat)
- Server pushes:
  - ``{"type": "tick",  symbol, ltp, change, change_pct, volume, ts}``
  - ``{"type": "ping"}`` every 15 seconds
  - ``{"type": "error", "detail": "..."}`` on bad client input

Integration: import :data:`stream_router` and call
``app.include_router(stream_router)`` from ``app/main.py``. (Not edited
here per task ownership — exported only.)
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.services.streaming.quote_stream import (
    QuoteHub,
    _Subscriber,
    get_quote_hub,
)

# Subprotocol prefix used to smuggle the API key through the
# `Sec-WebSocket-Protocol` header — browsers don't allow custom headers on
# the WebSocket constructor, so we (ab)use the subprotocol mechanism to
# carry the key. Format: `agentx.key.<API_KEY>`.
_SUBPROTOCOL_KEY_PREFIX = "agentx.key."

logger = logging.getLogger(__name__)

# Heartbeat cadence — server → client.
_HEARTBEAT_SECS = 15.0

# How long to wait for a single outbound queue read before re-checking
# heartbeat / connection state.
_QUEUE_READ_TIMEOUT_SECS = 1.0

# Cap on inbound message size to prevent abuse.
_MAX_INBOUND_BYTES = 4096

# Cap on symbols a single connection may track.
_MAX_SYMBOLS_PER_CONN = 100


stream_router = APIRouter(prefix="/api/stream", tags=["stream"])


def _parse_symbols(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def _extract_api_key(websocket: WebSocket, query_key: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Pull the API key from (in order):
      1. ``Sec-WebSocket-Protocol`` subprotocol of the form ``agentx.key.<KEY>``
         — the only mechanism browsers allow for adding auth material on a
         WebSocket upgrade without an extra round-trip.
      2. Query string ``?api_key=<KEY>`` — fallback for non-browser clients
         (Python, curl, scripts).
      3. ``X-API-Key`` HTTP header — works only for non-browser clients but
         keeps parity with the REST contract.

    Returns ``(key, matched_subprotocol)``. ``matched_subprotocol`` is the
    full subprotocol string we should echo back on accept (so the client's
    WebSocket handshake doesn't fail), or ``None`` if the key arrived via
    query/header.
    """
    # 1. Subprotocol header (comma-separated list per RFC 6455)
    raw_proto = websocket.headers.get("sec-websocket-protocol", "")
    for proto in (p.strip() for p in raw_proto.split(",") if p.strip()):
        if proto.startswith(_SUBPROTOCOL_KEY_PREFIX):
            return proto[len(_SUBPROTOCOL_KEY_PREFIX):], proto
    # 2. Query string
    if query_key:
        return query_key, None
    # 3. Header (non-browser)
    header_key = websocket.headers.get("x-api-key")
    if header_key:
        return header_key, None
    return None, None


@stream_router.websocket("/quotes")
async def stream_quotes(
    websocket: WebSocket,
    symbols: Optional[str] = Query(default=None, description="Comma-separated symbols"),
    api_key: Optional[str] = Query(default=None, description="API key (fallback for non-browser clients)"),
) -> None:
    """Stream real-time price ticks for one or more symbols.

    Authenticates the upgrade request using the same X-API-Key that protects
    the HTTP API. ASGI HTTP middleware does not run for WS upgrades, so the
    check is enforced inline here. See ADR-002.
    """
    # Auth must happen BEFORE accept(); otherwise the handshake completes and
    # we'd be leaking access for the duration of the close round-trip.
    from app.main import verify_api_key  # local import to avoid circular import at module load

    provided, matched_proto = _extract_api_key(websocket, api_key)
    if not verify_api_key(provided):
        logger.warning(
            "ws auth failed (key_present=%s, via_subprotocol=%s)",
            bool(provided), matched_proto is not None,
        )
        await websocket.close(code=4401, reason="unauthorized")
        return

    # Accept — echo the subprotocol back when the key arrived that way, else
    # accept with no subprotocol (browser will see the empty `protocol` field
    # which is fine when the constructor wasn't given a list).
    if matched_proto is not None:
        await websocket.accept(subprotocol=matched_proto)
    else:
        await websocket.accept()
    hub: QuoteHub = get_quote_hub()
    sub = _Subscriber()
    await hub.add_subscriber(sub)

    initial = _parse_symbols(symbols)
    if initial:
        await hub.subscribe(sub, initial[:_MAX_SYMBOLS_PER_CONN])

    sender_task = asyncio.create_task(_sender_loop(websocket, sub), name="ws-sender")
    receiver_task = asyncio.create_task(
        _receiver_loop(websocket, hub, sub), name="ws-receiver"
    )

    try:
        # Whichever finishes first → we tear down both.
        done, pending = await asyncio.wait(
            {sender_task, receiver_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    finally:
        await hub.remove_subscriber(sub)
        if sub.dropped:
            logger.warning(
                "ws closed with %d dropped messages (slow consumer)", sub.dropped
            )
        if websocket.client_state != WebSocketState.DISCONNECTED:
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass


async def _sender_loop(websocket: WebSocket, sub: _Subscriber) -> None:
    """Drain the subscriber queue → ws, plus 15s heartbeat."""
    try:
        while True:
            try:
                msg = await asyncio.wait_for(
                    sub.queue.get(), timeout=_HEARTBEAT_SECS
                )
            except asyncio.TimeoutError:
                # No tick within heartbeat window — send ping.
                await _safe_send_json(websocket, {"type": "ping"})
                continue
            if not await _safe_send_json(websocket, msg):
                return
    except asyncio.CancelledError:
        raise


async def _receiver_loop(
    websocket: WebSocket, hub: QuoteHub, sub: _Subscriber,
) -> None:
    """Handle subscribe/unsubscribe/pong frames from the client."""
    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                return
            if len(raw) > _MAX_INBOUND_BYTES:
                await _safe_send_json(
                    websocket, {"type": "error", "detail": "message too large"}
                )
                continue
            try:
                payload: dict[str, Any] = json.loads(raw)
            except json.JSONDecodeError:
                await _safe_send_json(
                    websocket, {"type": "error", "detail": "invalid JSON"}
                )
                continue

            mtype = payload.get("type")
            if mtype == "subscribe":
                syms = _coerce_symbols(payload.get("symbols"))
                if syms:
                    # Enforce per-connection cap.
                    headroom = _MAX_SYMBOLS_PER_CONN - len(sub.symbols)
                    if headroom > 0:
                        await hub.subscribe(sub, syms[:headroom])
            elif mtype == "unsubscribe":
                syms = _coerce_symbols(payload.get("symbols"))
                if syms:
                    await hub.unsubscribe(sub, syms)
            elif mtype == "pong":
                # Liveness ack — nothing to do.
                continue
            else:
                await _safe_send_json(
                    websocket, {"type": "error", "detail": f"unknown type: {mtype}"}
                )
    except asyncio.CancelledError:
        raise


def _coerce_symbols(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip().upper())
    return out


async def _safe_send_json(websocket: WebSocket, msg: dict[str, Any]) -> bool:
    """Best-effort JSON send. Returns False on failure (connection dead)."""
    if websocket.client_state != WebSocketState.CONNECTED:
        return False
    try:
        await websocket.send_text(json.dumps(msg))
        return True
    except Exception:  # noqa: BLE001 — disconnects come in many flavours
        return False


# NOTE FOR INTEGRATION (not done in this file per ownership):
#   In app/main.py, after the existing router includes, add:
#       from app.routers.stream import stream_router
#       app.include_router(stream_router)
#   And in the lifespan shutdown phase, call:
#       from app.services.streaming import get_quote_hub
#       await get_quote_hub().shutdown()

"""WebSocket authentication tests for ``/api/stream/quotes``.

Covers ADR-002 fix: HTTP middleware does not run on WS upgrades, so the
``X-API-Key`` check must be enforced inline by the WS handler. These tests
exercise:

  * connect without any key                        → close(4401)
  * connect with a valid key via ``?api_key=``     → accepts and ticks flow
  * connect with a valid key via subprotocol       → accepts; subprotocol echoed
  * connect with an invalid key                    → close(4401)
  * AGENTX_DEV=1 bypass                            → accepts without a key
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import settings
from app.routers.stream import stream_router
from app.services.streaming.quote_stream import QuoteHub

# Reuse the in-test stub source from the existing stream test module so we
# don't reimplement the fanout fixture.
from tests.test_stream import _StubSource, _wait_for


_VALID_KEY = "test-secret-key-123"


@pytest.fixture
def app_with_stub_authed(monkeypatch):
    """Same shape as ``test_stream.app_with_stub`` but with auth enabled."""
    from app.services.streaming import quote_stream as qs

    hub = QuoteHub()
    stub = _StubSource()
    hub.attach_source(stub)
    monkeypatch.setattr(qs, "_HUB", hub)

    # Enable auth for the duration of the test.
    monkeypatch.setattr(settings, "api_key", _VALID_KEY)
    # Ensure dev bypass is OFF for normal cases — individual tests opt in.
    monkeypatch.delenv("AGENTX_DEV", raising=False)

    app = FastAPI()
    app.include_router(stream_router)
    return app, hub, stub


def test_ws_rejects_connection_without_api_key(app_with_stub_authed):
    app, _hub, _stub = app_with_stub_authed
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/api/stream/quotes") as ws:
                # Server should close before any frame is delivered.
                ws.receive_text()
        assert exc_info.value.code == 4401


def test_ws_rejects_invalid_api_key(app_with_stub_authed):
    app, _hub, _stub = app_with_stub_authed
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/api/stream/quotes?api_key=wrong-key"
            ) as ws:
                ws.receive_text()
        assert exc_info.value.code == 4401


def test_ws_accepts_valid_api_key_via_query_param(app_with_stub_authed):
    app, _hub, stub = app_with_stub_authed
    with TestClient(app) as client:
        url = f"/api/stream/quotes?symbols=RELIANCE&api_key={_VALID_KEY}"
        with client.websocket_connect(url) as ws:
            _wait_for(lambda: "RELIANCE" in stub.subscribed)
            asyncio.run(stub.emit("RELIANCE", 100.0))
            msg = ws.receive_json()
            assert msg["type"] == "tick"
            assert msg["symbol"] == "RELIANCE"


def test_ws_accepts_valid_api_key_via_subprotocol(app_with_stub_authed):
    app, _hub, stub = app_with_stub_authed
    subprotocol = f"agentx.key.{_VALID_KEY}"
    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/stream/quotes?symbols=TCS",
            subprotocols=[subprotocol],
        ) as ws:
            # The server must echo the subprotocol back so the browser handshake
            # succeeds. Starlette's TestClient surfaces the accepted value on
            # ``accepted_subprotocol``.
            assert ws.accepted_subprotocol == subprotocol
            _wait_for(lambda: "TCS" in stub.subscribed)
            asyncio.run(stub.emit("TCS", 250.0))
            msg = ws.receive_json()
            assert msg["symbol"] == "TCS"


def test_ws_dev_mode_bypass(monkeypatch):
    """When AGENTX_DEV=1, the WS endpoint accepts without a key even if
    ``settings.api_key`` is set."""
    from app.services.streaming import quote_stream as qs

    hub = QuoteHub()
    stub = _StubSource()
    hub.attach_source(stub)
    monkeypatch.setattr(qs, "_HUB", hub)
    monkeypatch.setattr(settings, "api_key", _VALID_KEY)
    monkeypatch.setenv("AGENTX_DEV", "1")

    app = FastAPI()
    app.include_router(stream_router)

    with TestClient(app) as client:
        with client.websocket_connect("/api/stream/quotes?symbols=INFY") as ws:
            _wait_for(lambda: "INFY" in stub.subscribed)
            asyncio.run(stub.emit("INFY", 1500.0))
            msg = ws.receive_json()
            assert msg["symbol"] == "INFY"

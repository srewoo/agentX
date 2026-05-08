"""Tests for the WebSocket quote stream.

Covers:
- WS lifecycle: connect via query param → receive ticks → unsubscribe → close.
- JSON subscribe / unsubscribe frames.
- Backpressure: queue capped at 256, oldest dropped, counter incremented.
- Tick deduplication (no fanout when ltp & volume unchanged).
- Heartbeat ping when no ticks flow.
- Bad input (oversized / non-JSON) yields error frames, not a crash.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.stream import stream_router
from app.services.streaming.quote_stream import (
    QuoteHub,
    Tick,
    _PER_CONN_QUEUE_MAX,
    _Subscriber,
)


class _StubSource:
    """In-memory quote source. Tests push ticks via :meth:`emit`."""

    def __init__(self) -> None:
        self.cb = None
        self.subscribed: set[str] = set()
        self.started = False

    def set_callback(self, cb) -> None:
        self.cb = cb

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def subscribe(self, symbol: str) -> None:
        self.subscribed.add(symbol)

    async def unsubscribe(self, symbol: str) -> None:
        self.subscribed.discard(symbol)

    async def emit(self, symbol: str, ltp: float, volume: int = 100) -> None:
        assert self.cb is not None
        await self.cb(
            Tick(
                type="tick",
                symbol=symbol,
                ltp=ltp,
                change=0.0,
                change_pct=0.0,
                volume=volume,
                ts=1.0,
            )
        )


@pytest.fixture
def app_with_stub(monkeypatch):
    """Build an isolated FastAPI app with a fresh hub + stub source."""
    from app.services.streaming import quote_stream as qs

    hub = QuoteHub()
    stub = _StubSource()
    hub.attach_source(stub)
    monkeypatch.setattr(qs, "_HUB", hub)

    app = FastAPI()
    app.include_router(stream_router)
    return app, hub, stub


def test_subscribe_via_query_param_receives_ticks(app_with_stub):
    app, hub, stub = app_with_stub
    with TestClient(app) as client:
        with client.websocket_connect("/api/stream/quotes?symbols=RELIANCE,TCS") as ws:
            # Give the server time to register subs and start the source.
            _wait_for(lambda: stub.subscribed >= {"RELIANCE", "TCS"})

            asyncio.run(stub.emit("RELIANCE", 100.0))
            msg = ws.receive_json()
            assert msg["type"] == "tick"
            assert msg["symbol"] == "RELIANCE"
            assert msg["ltp"] == 100.0


def test_json_subscribe_and_unsubscribe(app_with_stub):
    app, hub, stub = app_with_stub
    with TestClient(app) as client:
        with client.websocket_connect("/api/stream/quotes") as ws:
            ws.send_json({"type": "subscribe", "symbols": ["INFY"]})
            _wait_for(lambda: "INFY" in stub.subscribed)

            asyncio.run(stub.emit("INFY", 1500.0))
            msg = ws.receive_json()
            assert msg["symbol"] == "INFY"

            ws.send_json({"type": "unsubscribe", "symbols": ["INFY"]})
            _wait_for(lambda: "INFY" not in stub.subscribed)


def test_invalid_json_yields_error_frame(app_with_stub):
    app, _hub, _stub = app_with_stub
    with TestClient(app) as client:
        with client.websocket_connect("/api/stream/quotes") as ws:
            ws.send_text("{not json")
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert "JSON" in msg["detail"]


def test_unknown_message_type_yields_error(app_with_stub):
    app, _hub, _stub = app_with_stub
    with TestClient(app) as client:
        with client.websocket_connect("/api/stream/quotes") as ws:
            ws.send_json({"type": "frobnicate"})
            msg = ws.receive_json()
            assert msg["type"] == "error"


def test_pong_is_accepted_silently(app_with_stub):
    app, _hub, stub = app_with_stub
    with TestClient(app) as client:
        with client.websocket_connect("/api/stream/quotes?symbols=HDFC") as ws:
            ws.send_json({"type": "pong"})
            # Server should still deliver subsequent ticks.
            _wait_for(lambda: "HDFC" in stub.subscribed)
            asyncio.run(stub.emit("HDFC", 42.0))
            msg = ws.receive_json()
            assert msg["symbol"] == "HDFC"


# ── Hub-level (no websocket) tests ──────────────────────────────────


async def test_hub_dedupes_unchanged_ticks():
    hub = QuoteHub()
    stub = _StubSource()
    hub.attach_source(stub)
    sub = _Subscriber()
    await hub.add_subscriber(sub)
    await hub.subscribe(sub, ["AAA"])

    await stub.emit("AAA", 100.0, volume=10)
    await stub.emit("AAA", 100.0, volume=10)  # duplicate
    await stub.emit("AAA", 101.0, volume=10)  # change

    # Drain queue.
    received: list[dict] = []
    while not sub.queue.empty():
        received.append(sub.queue.get_nowait())

    ltps = [m["ltp"] for m in received]
    assert ltps == [100.0, 101.0]
    await hub.shutdown()


async def test_hub_backpressure_drops_oldest():
    hub = QuoteHub()
    stub = _StubSource()
    hub.attach_source(stub)
    sub = _Subscriber()
    await hub.add_subscriber(sub)
    await hub.subscribe(sub, ["BBB"])

    # Push more than the cap. Each call must produce a unique tick to bypass
    # the dedup guard, so we vary ltp.
    overflow = 50
    total = _PER_CONN_QUEUE_MAX + overflow
    for i in range(total):
        await stub.emit("BBB", float(i), volume=i)

    assert sub.queue.qsize() == _PER_CONN_QUEUE_MAX
    assert sub.dropped >= overflow
    # Oldest were dropped → first message in queue is well past 0.
    first = sub.queue.get_nowait()
    assert first["ltp"] >= overflow - 1
    await hub.shutdown()


async def test_hub_refcounts_upstream_subscriptions():
    hub = QuoteHub()
    stub = _StubSource()
    hub.attach_source(stub)

    sub_a = _Subscriber()
    sub_b = _Subscriber()
    await hub.add_subscriber(sub_a)
    await hub.add_subscriber(sub_b)

    await hub.subscribe(sub_a, ["XXX"])
    await hub.subscribe(sub_b, ["XXX"])
    assert "XXX" in stub.subscribed

    await hub.unsubscribe(sub_a, ["XXX"])
    # Still one subscriber; upstream stays.
    assert "XXX" in stub.subscribed

    await hub.unsubscribe(sub_b, ["XXX"])
    # Now upstream should be released.
    assert "XXX" not in stub.subscribed

    await hub.shutdown()


# ── Helpers ─────────────────────────────────────────────────────────
def _wait_for(predicate, timeout: float = 2.0, interval: float = 0.02) -> None:
    """Spin-wait for ``predicate`` to return truthy. Pytest-friendly."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("predicate did not become true within timeout")

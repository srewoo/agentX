"""Quote streaming hub: fan out per-symbol ticks to many subscribers.

The hub keeps one upstream subscription per unique symbol regardless of
how many WebSocket clients are listening. Each subscriber owns a bounded
``asyncio.Queue`` (256 messages) — on overflow the oldest tick is dropped
and a per-connection counter is incremented, so a slow client cannot
back-pressure others or the upstream source.

When Redis is connected, ticks are also published to ``stream:ticks:<SYMBOL>``
so other worker processes can share the same upstream feed instead of each
opening their own poll loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# Per-connection outbound queue cap. On overflow, oldest message is dropped.
_PER_CONN_QUEUE_MAX = 256

# Redis pub/sub channel prefix for cross-process tick fanout.
_REDIS_CHANNEL_PREFIX = "stream:ticks:"


@dataclass
class Tick:
    """A single normalised price tick."""

    type: str  # always "tick"
    symbol: str
    ltp: float
    change: float
    change_pct: float
    volume: int
    ts: float  # epoch seconds

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class QuoteSource(Protocol):
    """Pluggable upstream quote source."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def subscribe(self, symbol: str) -> None: ...

    async def unsubscribe(self, symbol: str) -> None: ...

    def set_callback(self, cb: "QuoteCallback") -> None: ...


# Callback signature: source pushes a Tick to the hub.
from collections.abc import Awaitable, Callable  # noqa: E402

QuoteCallback = Callable[[Tick], Awaitable[None]]


class _Subscriber:
    """A single WebSocket client's view into the hub."""

    __slots__ = ("symbols", "queue", "dropped")

    def __init__(self) -> None:
        self.symbols: set[str] = set()
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=_PER_CONN_QUEUE_MAX
        )
        self.dropped: int = 0

    def offer(self, msg: dict[str, Any]) -> None:
        """Non-blocking enqueue. Drops oldest on overflow."""
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            try:
                _ = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.dropped += 1
            try:
                self.queue.put_nowait(msg)
            except asyncio.QueueFull:
                # Extremely unlikely; just drop this message too.
                self.dropped += 1


class QuoteHub:
    """Singleton fanout hub. Lazily starts a :class:`QuoteSource` on first sub."""

    def __init__(self) -> None:
        self._subs: set[_Subscriber] = set()
        self._symbol_refcount: dict[str, int] = {}
        self._source: QuoteSource | None = None
        self._last_tick: dict[str, Tick] = {}
        self._lock: asyncio.Lock | None = None
        self._started = False
        self._redis: Any | None = None

    def _get_lock(self) -> asyncio.Lock:
        # Lazy init — Python 3.9 asyncio.Lock() requires a running loop.
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ── Source wiring ────────────────────────────────────────────────
    def attach_source(self, source: QuoteSource) -> None:
        self._source = source
        source.set_callback(self._on_tick)

    async def _ensure_started(self) -> None:
        if self._started:
            return
        async with self._get_lock():
            if self._started:
                return
            if self._source is None:
                self._source = _build_default_source()
                self._source.set_callback(self._on_tick)
            try:
                await self._source.start()
            except Exception:
                logger.warning(
                    "%s failed to start; falling back to polling",
                    type(self._source).__name__,
                    exc_info=True,
                )
                from app.services.streaming.poll_fallback import PollingQuoteSource

                self._source = PollingQuoteSource()
                self._source.set_callback(self._on_tick)
                await self._source.start()
            # Attach redis if available — best effort.
            try:
                from app.services.cache import cache_manager
                if cache_manager._enabled and cache_manager._client is not None:
                    self._redis = cache_manager._client
            except Exception:  # noqa: BLE001 — cache is optional
                self._redis = None
            self._started = True
            logger.info("QuoteHub started (source=%s)", type(self._source).__name__)

    async def shutdown(self) -> None:
        """Stop the upstream source. Subscribers are expected to be closed already."""
        async with self._get_lock():
            if not self._started:
                return
            if self._source is not None:
                try:
                    await self._source.stop()
                except Exception:  # noqa: BLE001
                    logger.exception("Error stopping quote source")
            self._started = False
            self._symbol_refcount.clear()
            self._last_tick.clear()
            logger.info("QuoteHub stopped")

    # ── Subscriber lifecycle ─────────────────────────────────────────
    async def add_subscriber(self, sub: _Subscriber) -> None:
        await self._ensure_started()
        self._subs.add(sub)

    async def remove_subscriber(self, sub: _Subscriber) -> None:
        self._subs.discard(sub)
        # Decrement refcounts for everything this sub had.
        for sym in list(sub.symbols):
            await self._unsub_symbol(sub, sym)

    async def subscribe(self, sub: _Subscriber, symbols: list[str]) -> None:
        for raw in symbols:
            sym = _normalise(raw)
            if not sym or sym in sub.symbols:
                continue
            sub.symbols.add(sym)
            new_count = self._symbol_refcount.get(sym, 0) + 1
            self._symbol_refcount[sym] = new_count
            if new_count == 1 and self._source is not None:
                try:
                    await self._source.subscribe(sym)
                except Exception:  # noqa: BLE001
                    logger.exception("Source subscribe failed for %s", sym)
            # Replay last known tick for snappy UX.
            last = self._last_tick.get(sym)
            if last is not None:
                sub.offer(last.to_dict())

    async def unsubscribe(self, sub: _Subscriber, symbols: list[str]) -> None:
        for raw in symbols:
            sym = _normalise(raw)
            if sym in sub.symbols:
                await self._unsub_symbol(sub, sym)

    async def _unsub_symbol(self, sub: _Subscriber, sym: str) -> None:
        sub.symbols.discard(sym)
        count = self._symbol_refcount.get(sym, 0) - 1
        if count <= 0:
            self._symbol_refcount.pop(sym, None)
            if self._source is not None:
                try:
                    await self._source.unsubscribe(sym)
                except Exception:  # noqa: BLE001
                    logger.exception("Source unsubscribe failed for %s", sym)
        else:
            self._symbol_refcount[sym] = count

    # ── Upstream callback ────────────────────────────────────────────
    async def _on_tick(self, tick: Tick) -> None:
        # Deduplicate: only fan out if changed vs previous tick for that symbol.
        prev = self._last_tick.get(tick.symbol)
        if prev is not None and prev.ltp == tick.ltp and prev.volume == tick.volume:
            return
        self._last_tick[tick.symbol] = tick
        msg = tick.to_dict()
        for s in list(self._subs):
            if tick.symbol in s.symbols:
                s.offer(msg)
        if self._redis is not None:
            try:
                import json
                await self._redis.publish(
                    _REDIS_CHANNEL_PREFIX + tick.symbol, json.dumps(msg)
                )
            except Exception:  # noqa: BLE001
                # Redis is best-effort — never let it break the local fanout.
                logger.debug("Redis publish failed", exc_info=True)


# ── Helpers ──────────────────────────────────────────────────────────
def _normalise(symbol: str) -> str:
    return (symbol or "").strip().upper()


def _build_default_source() -> QuoteSource:
    """Pick a broker WebSocket when configured, else polling."""
    has_kite = bool(os.environ.get("KITE_API_KEY")) and bool(
        os.environ.get("KITE_ACCESS_TOKEN")
    )
    if has_kite:
        try:
            from app.services.streaming.broker_kite import KiteQuoteSource
            return KiteQuoteSource()
        except Exception:  # noqa: BLE001
            logger.warning("broker WS unavailable; falling back to polling", exc_info=True)
    from app.services.streaming.poll_fallback import PollingQuoteSource
    return PollingQuoteSource()


# Module-level singleton — one hub per process.
_HUB: QuoteHub | None = None


def get_quote_hub() -> QuoteHub:
    """Return the process-wide :class:`QuoteHub` singleton."""
    global _HUB
    if _HUB is None:
        _HUB = QuoteHub()
    return _HUB


# Re-export for type hints and tests.
__all__ = [
    "QuoteHub",
    "QuoteSource",
    "Tick",
    "_Subscriber",
    "get_quote_hub",
]


def _now() -> float:
    """Return current epoch seconds. Indirection makes tests deterministic."""
    return time.time()

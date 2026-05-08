"""Zerodha Kite broker WebSocket source — STUB.

This module exists so the hub can route to a real broker feed when
``KITE_API_KEY`` and ``KITE_ACCESS_TOKEN`` are configured. The actual
KiteTicker integration is not implemented yet — calling :meth:`start`
raises :class:`NotImplementedError`. The hub catches that and falls
back to polling, logging "broker WS unavailable".
"""
from __future__ import annotations

import logging
import os

from app.services.streaming.quote_stream import QuoteCallback, Tick  # noqa: F401

logger = logging.getLogger(__name__)


class KiteQuoteSource:
    """Stub for the Zerodha KiteTicker websocket integration."""

    def __init__(self) -> None:
        self._api_key = os.environ.get("KITE_API_KEY", "")
        self._access_token = os.environ.get("KITE_ACCESS_TOKEN", "")
        self._cb: QuoteCallback | None = None

    def set_callback(self, cb: QuoteCallback) -> None:
        self._cb = cb

    async def start(self) -> None:
        # Intentionally not implemented yet. The hub guards this and
        # falls back to PollingQuoteSource on failure.
        logger.warning("broker WS unavailable: KiteQuoteSource is a stub")
        raise NotImplementedError("KiteQuoteSource is not implemented yet")

    async def stop(self) -> None:
        return None

    async def subscribe(self, symbol: str) -> None:
        return None

    async def unsubscribe(self, symbol: str) -> None:
        return None

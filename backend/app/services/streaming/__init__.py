"""Real-time quote streaming subsystem.

A single :class:`QuoteHub` fans out per-symbol ticks from a pluggable
:class:`QuoteSource` (broker websocket OR yfinance polling) to many
WebSocket subscribers. One upstream subscription per unique symbol.

The default source is :class:`PollingQuoteSource` (yfinance polling at
~2.5s cadence) — works out of the box. When ``KITE_API_KEY`` and
``KITE_ACCESS_TOKEN`` env vars are present, :class:`KiteQuoteSource` is
selected (currently a stub — see ``broker_kite.py``).
"""
from __future__ import annotations

from app.services.streaming.quote_stream import (
    QuoteHub,
    QuoteSource,
    Tick,
    get_quote_hub,
)

__all__ = ["QuoteHub", "QuoteSource", "Tick", "get_quote_hub"]

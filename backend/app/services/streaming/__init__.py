"""Real-time quote streaming subsystem.

A single :class:`QuoteHub` fans out per-symbol ticks from a pluggable
:class:`QuoteSource` (broker websocket OR yfinance polling) to many
WebSocket subscribers. One upstream subscription per unique symbol.

The default source is :class:`PollingQuoteSource` (public quote polling at
~2.5s cadence) — works out of the box. Kite remains a later broker option.
"""
from __future__ import annotations

from app.services.streaming.quote_stream import (
    QuoteHub,
    QuoteSource,
    Tick,
    get_quote_hub,
)

__all__ = ["QuoteHub", "QuoteSource", "Tick", "get_quote_hub"]

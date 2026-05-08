"""Polling-based quote source — yfinance ``fast_info`` at ~2.5s cadence.

Used when no broker WebSocket is configured. One asyncio task per
unique subscribed symbol; sync yfinance calls run in the default
executor with a hard timeout so a wedged HTTP call cannot block the
event loop.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.services.streaming.quote_stream import QuoteCallback, Tick, _now

logger = logging.getLogger(__name__)

# Polling cadence between successive fetches for the SAME symbol.
_POLL_INTERVAL_SECS = 2.5

# Hard timeout for a single yfinance fetch — keeps a stuck HTTP call from
# blocking the executor pool indefinitely.
_FETCH_TIMEOUT_SECS = 5.0

# yfinance Indian-market suffixes to try in order.
_NSE_SUFFIX = ".NS"
_BSE_SUFFIX = ".BO"


def _fetch_quote_sync(symbol: str) -> dict[str, Any] | None:
    """Blocking yfinance fetch. Returns a dict or None on failure.

    Tries NSE (.NS) then BSE (.BO). Uses ``fast_info`` (cheap) and falls
    back to ``info`` only when fast_info is missing fields.
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not installed; polling source disabled")
        return None

    for suffix in (_NSE_SUFFIX, _BSE_SUFFIX):
        yf_sym = f"{symbol}{suffix}"
        try:
            ticker = yf.Ticker(yf_sym)
            fi = ticker.fast_info
            ltp = _safe_float(getattr(fi, "last_price", None))
            prev_close = _safe_float(getattr(fi, "previous_close", None))
            volume = _safe_int(getattr(fi, "last_volume", None))
            if ltp is None or prev_close is None:
                continue
            change = ltp - prev_close
            change_pct = (change / prev_close * 100.0) if prev_close else 0.0
            return {
                "ltp": ltp,
                "change": change,
                "change_pct": change_pct,
                "volume": volume or 0,
            }
        except Exception as e:  # noqa: BLE001 — yfinance throws a zoo of errors
            logger.debug("fast_info failed for %s: %s", yf_sym, e)
            continue
    return None


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f else None  # NaN check
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> int | None:
    try:
        if v is None:
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


class PollingQuoteSource:
    """Per-symbol asyncio task polling yfinance at :data:`_POLL_INTERVAL_SECS`."""

    def __init__(
        self,
        interval_secs: float = _POLL_INTERVAL_SECS,
        fetch_timeout_secs: float = _FETCH_TIMEOUT_SECS,
    ) -> None:
        self._interval = interval_secs
        self._timeout = fetch_timeout_secs
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cb: QuoteCallback | None = None
        self._stopped = False

    def set_callback(self, cb: QuoteCallback) -> None:
        self._cb = cb

    async def start(self) -> None:
        self._stopped = False
        logger.info(
            "PollingQuoteSource started (interval=%.1fs)", self._interval
        )

    async def stop(self) -> None:
        self._stopped = True
        tasks = list(self._tasks.values())
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()

    async def subscribe(self, symbol: str) -> None:
        if symbol in self._tasks or self._stopped:
            return
        self._tasks[symbol] = asyncio.create_task(
            self._poll_loop(symbol), name=f"poll-{symbol}"
        )

    async def unsubscribe(self, symbol: str) -> None:
        task = self._tasks.pop(symbol, None)
        if task is not None:
            task.cancel()

    async def _poll_loop(self, symbol: str) -> None:
        loop = asyncio.get_event_loop()
        consecutive_failures = 0
        try:
            while not self._stopped:
                try:
                    quote = await asyncio.wait_for(
                        loop.run_in_executor(None, _fetch_quote_sync, symbol),
                        timeout=self._timeout,
                    )
                except asyncio.TimeoutError:
                    consecutive_failures += 1
                    logger.warning(
                        "poll timeout for %s (consecutive=%d)",
                        symbol, consecutive_failures,
                    )
                    quote = None
                except Exception:  # noqa: BLE001
                    consecutive_failures += 1
                    logger.exception("poll error for %s", symbol)
                    quote = None

                if quote is not None and self._cb is not None:
                    consecutive_failures = 0
                    tick = Tick(
                        type="tick",
                        symbol=symbol,
                        ltp=quote["ltp"],
                        change=quote["change"],
                        change_pct=quote["change_pct"],
                        volume=quote["volume"],
                        ts=_now(),
                    )
                    try:
                        await self._cb(tick)
                    except Exception:  # noqa: BLE001
                        logger.exception("hub callback raised for %s", symbol)

                # Backoff on repeated failure to avoid hammering yfinance.
                delay = self._interval
                if consecutive_failures > 3:
                    delay = min(self._interval * consecutive_failures, 30.0)
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise

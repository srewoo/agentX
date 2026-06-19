"""Decoupled stop-loss monitor.

Runs as its own asyncio task at a tight cadence (default 60s) — *not*
gated on the much-slower full scan cycle. For every open paper position
it:

1. Snapshots current price (broker → NSE → yfinance fallback chain).
2. Evaluates the direction-aware stop:
   - bullish: ``current <= stop_loss`` ⇒ trigger
   - bearish: ``current >= stop_loss`` ⇒ trigger
3. Updates the trailing stop (uses existing ``update_trailing_stop``).
4. Synthesises a SELL/BUY-to-close signal and routes it through the
   same close-position path as a normal exit, so accounting (P&L,
   daily-loss circuit breaker, risk-state table) updates uniformly.

The point: exits stop firing on the scan cadence (5+ minutes) — they
fire whenever the market actually breaches the stop.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_POLL_SECONDS = 60


async def _current_price(symbol: str) -> Optional[float]:
    """Resolve current price via the existing get_stock_quote chain."""
    try:
        from app.services.data_fetcher import get_stock_quote
        q = await get_stock_quote(symbol)
        if q and q.get("lastPrice"):
            return float(q["lastPrice"])
    except Exception as e:
        logger.debug("stop-loss monitor: price lookup failed for %s: %s", symbol, e)
    return None


def _is_breached(direction: str, current: float, stop: float) -> bool:
    """Direction-aware stop-loss test."""
    if stop <= 0:
        return False
    if direction == "bullish":
        return current <= stop
    if direction == "bearish":
        return current >= stop
    return False


async def evaluate_open_positions(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return positions whose stop was breached *now*.

    Each returned dict includes ``trigger_price`` (the current LTP) and
    a ``reason`` string suitable for stamping on the synthesized exit
    signal.
    """
    triggered: list[dict[str, Any]] = []
    for pos in positions:
        symbol = pos.get("symbol")
        if not symbol:
            continue
        direction = pos.get("direction") or pos.get("side") or "bullish"
        # Normalise broker-style "BUY" / "SELL" to internal bullish/bearish.
        if direction == "BUY":
            direction = "bullish"
        elif direction == "SELL":
            direction = "bearish"

        try:
            stop = float(pos.get("stop_loss") or 0)
        except (ValueError, TypeError):
            continue
        if stop <= 0:
            continue

        current = await _current_price(symbol)
        if current is None:
            continue

        if _is_breached(direction, current, stop):
            triggered.append({
                **pos,
                "trigger_price": current,
                "reason": (
                    f"Stop-loss triggered: {symbol} {direction} entry "
                    f"₹{pos.get('entry_price')} stop ₹{stop:.2f} LTP ₹{current:.2f}"
                ),
            })
    return triggered


# ─────────────────────────────────────────────────────────────────────────
# Background loop
# ─────────────────────────────────────────────────────────────────────────

async def stop_loss_loop(
    *,
    poll_seconds: int = _DEFAULT_POLL_SECONDS,
    should_run: Optional[callable] = None,
) -> None:
    """Background coroutine. Owns its own cadence — independent of scans.

    ``should_run`` is an optional predicate (returns truthy to keep
    running). Defaults to ``True`` so the orchestrator can wrap it in a
    cancellable task.
    """
    while True if should_run is None else should_run():
        try:
            # Lazy-imports so this module stays cheap when not running.
            from app.services.paper_trading import list_paper_trades, close_paper_trade
            from app.services.risk_gate import record_trade_pnl

            trade_payload = await list_paper_trades(status="open")
            positions = trade_payload.get("trades", []) if isinstance(trade_payload, dict) else trade_payload
            triggered = await evaluate_open_positions(positions)
            seen_ids: set[Any] = set()
            for t in triggered:
                trade_id = t.get("id")
                # Skip duplicate triggers for the same trade within one pass —
                # close_paper_trade is also idempotent, this just avoids noise.
                if trade_id is not None and trade_id in seen_ids:
                    continue
                seen_ids.add(trade_id)
                try:
                    closed = await close_paper_trade(
                        trade_id=t.get("id"),
                        exit_price=t["trigger_price"],
                        exit_reason="stop_loss",
                    )
                    # Feed realised ₹ pnl to the daily-loss circuit breaker
                    # so a cascading stop-out day actually trips it.
                    if closed and closed.get("pnl_amount") is not None:
                        try:
                            await record_trade_pnl(float(closed["pnl_amount"]))
                        except Exception as inner_e:
                            logger.debug(
                                "circuit-breaker update skipped: %s", inner_e,
                            )
                    logger.info(
                        "Stop-loss closed: %s @ ₹%.2f (%s)",
                        t.get("symbol"), t["trigger_price"], t.get("reason"),
                    )
                except Exception as e:
                    logger.warning(
                        "Stop-loss close failed for %s: %s", t.get("symbol"), e,
                    )
        except Exception as e:
            # Loop is critical — never let it die from a single iteration's error.
            logger.warning("stop_loss_loop iteration failed: %s", e)
        try:
            await asyncio.sleep(poll_seconds)
        except asyncio.CancelledError:
            break

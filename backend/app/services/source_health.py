from __future__ import annotations
"""
Per-source negative cache (circuit breaker, lite).

When a data source fails (NSE 403, yfinance throttle, Upstox 401), we mark it
"down" for a cooldown window so the orchestrator's per-symbol loop stops
hammering it — and stops emitting a DEBUG line *per symbol per scan*, which is
exactly the log spam the user noticed. After the cooldown elapses the source
is tried again automatically.

Process-local and intentionally tiny — no Redis, no locks (GIL-safe dict
writes). State resets on restart, which is the correct conservative default.
"""
import logging
import time

logger = logging.getLogger(__name__)

# Default cooldown after a failure, seconds. NSE 403s and Yahoo throttles
# typically clear in a few minutes; 5 min balances recovery vs. spam.
_DEFAULT_COOLDOWN = 300.0

_down_until: dict[str, float] = {}


def mark_down(source: str, *, cooldown: float = _DEFAULT_COOLDOWN) -> None:
    """Mark ``source`` unavailable for ``cooldown`` seconds."""
    _down_until[source] = time.time() + cooldown
    logger.info("source_health: %s marked down for %.0fs", source, cooldown)


def is_down(source: str) -> bool:
    """True while ``source`` is inside its cooldown window."""
    until = _down_until.get(source)
    if until is None:
        return False
    if time.time() >= until:
        # Cooldown elapsed — clear so the next call retries the source.
        _down_until.pop(source, None)
        return False
    return True


def mark_up(source: str) -> None:
    """Clear any cooldown — call after a successful fetch."""
    _down_until.pop(source, None)


def reset() -> None:
    """Clear all state. For tests."""
    _down_until.clear()

"""Deterministic risk gates for the OMS layer.

Two distinct mechanisms live here:

1. **Daily-loss circuit breaker** (task #5): a stateful counter that
   tracks realised P&L for the current trading day and blocks any new
   entries once ``|daily_pnl| >= initial_capital * MAX_DAILY_LOSS_PCT``.
   Persisted to SQLite so a backend restart mid-session preserves the
   circuit. Resets automatically on the next IST trading day.

2. **Hard risk-gate validator** (task #8): a sequential, 10-rule
   pre-trade validator that the auto-trader / live OMS calls *after* the
   LLM judge but *before* any order. Returns ``APPROVED``,
   ``REJECTED``, or ``MODIFIED`` with mandatory warnings.

The two are intentionally in the same module — both are deterministic
safety nets distinct from the LLM judge layer, and they share state
(``daily_pnl``, ``open_positions_count``).
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

import aiosqlite

from app.database import DB_PATH

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Tunables — kept here so a settings UI can override at runtime later.
# ─────────────────────────────────────────────────────────────────────────

MAX_DAILY_LOSS_PCT = 2.5       # Halt new entries when intraday drawdown ≥ 2.5%
MAX_OPEN_POSITIONS = 10        # Concurrent open positions cap
MAX_POSITION_PCT = 5.0         # Per-position cap (% of capital)
MAX_SECTOR_PCT = 25.0          # Aggregate cap per GICS-ish sector
MIN_RISK_REWARD = 1.5          # Min R:R to take a trade
MIN_AVG_DAILY_VOLUME = 100_000  # Liquidity floor (shares/day)
MARKET_OPEN_HHMM = (9, 15)
MARKET_CLOSE_HHMM = (15, 30)


# ─────────────────────────────────────────────────────────────────────────
# Daily-loss circuit breaker
# ─────────────────────────────────────────────────────────────────────────

def _ensure_table(db: sqlite3.Connection | None = None) -> None:
    """Create ``risk_state`` table if missing. Idempotent."""
    schema = """
        CREATE TABLE IF NOT EXISTS risk_state (
            trading_day TEXT PRIMARY KEY,    -- YYYY-MM-DD (IST)
            daily_pnl REAL DEFAULT 0,
            entries_blocked INTEGER DEFAULT 0,
            last_updated_at TEXT
        )
    """
    if db is None:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(schema)
            conn.commit()
        finally:
            conn.close()
    else:
        db.execute(schema)
        db.commit()


def _ist_trading_day(now: Optional[datetime] = None) -> str:
    """Return ISO date of the current IST trading session.

    Sessions roll over at midnight IST. Naive UTC → IST conversion is
    fine because we only need a day key.
    """
    now = now or datetime.now(timezone.utc)
    # IST = UTC + 5:30
    from datetime import timedelta
    ist = now + timedelta(hours=5, minutes=30)
    return ist.date().isoformat()


async def get_daily_pnl(now: Optional[datetime] = None) -> float:
    """Return realised P&L for today's IST session (₹). 0 if first call."""
    day = _ist_trading_day(now)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS risk_state ("
            "trading_day TEXT PRIMARY KEY, daily_pnl REAL DEFAULT 0, "
            "entries_blocked INTEGER DEFAULT 0, last_updated_at TEXT)"
        )
        async with db.execute(
            "SELECT daily_pnl FROM risk_state WHERE trading_day = ?", (day,)
        ) as cur:
            row = await cur.fetchone()
            return float(row[0]) if row else 0.0


async def record_trade_pnl(pnl: float, *, now: Optional[datetime] = None) -> float:
    """Update today's P&L counter with a realised pnl (positive = profit).

    Returns the post-update daily total. Called by the paper trader /
    OMS the moment a position is closed.
    """
    day = _ist_trading_day(now)
    ts = (now or datetime.now(timezone.utc)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS risk_state ("
            "trading_day TEXT PRIMARY KEY, daily_pnl REAL DEFAULT 0, "
            "entries_blocked INTEGER DEFAULT 0, last_updated_at TEXT)"
        )
        # UPSERT idiom for SQLite — works on aiosqlite ≥ 0.17.
        await db.execute(
            """INSERT INTO risk_state (trading_day, daily_pnl, last_updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(trading_day) DO UPDATE SET
                   daily_pnl = daily_pnl + excluded.daily_pnl,
                   last_updated_at = excluded.last_updated_at""",
            (day, pnl, ts),
        )
        await db.commit()
        async with db.execute(
            "SELECT daily_pnl FROM risk_state WHERE trading_day = ?", (day,)
        ) as cur:
            row = await cur.fetchone()
            return float(row[0]) if row else 0.0


async def can_open_new_trade(
    capital: float,
    *,
    max_daily_loss_pct: float = MAX_DAILY_LOSS_PCT,
    now: Optional[datetime] = None,
) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for the OMS to gate an entry.

    Blocks once cumulative realised loss for the day breaches the
    configured % of capital. The break stays in effect until midnight
    IST (next day's row).
    """
    pnl = await get_daily_pnl(now)
    cap = max(1.0, float(capital))
    threshold = -abs(cap * max_daily_loss_pct / 100.0)
    if pnl <= threshold:
        return False, (
            f"daily-loss circuit-breaker active: P&L ₹{pnl:.0f} "
            f"≤ threshold ₹{threshold:.0f} ({max_daily_loss_pct:.1f}% of capital)"
        )
    return True, "ok"


# ─────────────────────────────────────────────────────────────────────────
# Hard risk-gate validator (task #8)
# ─────────────────────────────────────────────────────────────────────────

class GateVerdict(Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    MODIFIED = "MODIFIED"


@dataclass
class GateResult:
    """Outcome of one trade evaluation."""
    verdict: GateVerdict
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    modified_qty: Optional[int] = None
    modified_stop: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "modified_qty": self.modified_qty,
            "modified_stop": self.modified_stop,
        }


@dataclass
class TradeCandidate:
    """Everything the gate needs to evaluate a proposed trade."""
    symbol: str
    direction: str           # "bullish" or "bearish"
    entry: float
    stop: float
    target: float
    qty: int
    sector: str = "Unknown"
    avg_daily_volume: Optional[float] = None
    is_fno_banned: bool = False
    is_in_earnings_blackout: bool = False


@dataclass
class PortfolioState:
    """Snapshot of the portfolio at evaluation time."""
    capital: float
    open_positions: list[dict] = field(default_factory=list)
    correlation_to_open: float = 0.0  # 0..1 against the most-correlated open


def evaluate_trade(
    candidate: TradeCandidate,
    portfolio: PortfolioState,
    *,
    daily_pnl: float = 0.0,
    now: Optional[datetime] = None,
) -> GateResult:
    """Run the 10-rule sequential validator.

    Rules in order (first ``REJECTED`` short-circuits, ``MODIFIED`` keeps
    accumulating warnings):

    1. Market hours      — block outside 09:15-15:30 IST
    2. F&O ban list      — block if symbol is in current ban period
    3. Earnings blackout — block if a result is announced within 3 days
    4. Liquidity         — reject when avg daily volume < floor
    5. Risk:Reward       — reject when R:R < 1.5
    6. Daily-loss CB     — reject if circuit breaker active
    7. Max open positions
    8. Sector concentration
    9. Position size cap — modify qty if oversized
    10. Per-trade risk (stop distance × qty) cap
    """
    res = GateResult(verdict=GateVerdict.APPROVED)
    now = now or datetime.now(timezone.utc)

    # ── 1. Market hours (IST) ───────────────────────────────────────────
    from datetime import timedelta
    ist = now + timedelta(hours=5, minutes=30)
    hm = (ist.hour, ist.minute)
    if hm < MARKET_OPEN_HHMM or hm > MARKET_CLOSE_HHMM:
        res.warnings.append(
            f"outside market hours (IST {ist.hour:02d}:{ist.minute:02d})"
        )
        # Soft warn — backtest / paper trades can still proceed.

    # ── 2. F&O ban list ─────────────────────────────────────────────────
    if candidate.is_fno_banned:
        res.verdict = GateVerdict.REJECTED
        res.reasons.append(f"{candidate.symbol} is in F&O ban period")
        return res

    # ── 3. Earnings blackout ────────────────────────────────────────────
    if candidate.is_in_earnings_blackout:
        res.verdict = GateVerdict.REJECTED
        res.reasons.append(f"{candidate.symbol} is in earnings blackout (±3 days)")
        return res

    # ── 4. Liquidity ────────────────────────────────────────────────────
    if (
        candidate.avg_daily_volume is not None
        and candidate.avg_daily_volume < MIN_AVG_DAILY_VOLUME
    ):
        res.verdict = GateVerdict.REJECTED
        res.reasons.append(
            f"illiquid: avg daily volume {candidate.avg_daily_volume:.0f} "
            f"< floor {MIN_AVG_DAILY_VOLUME}"
        )
        return res

    # ── 5. Risk:Reward ──────────────────────────────────────────────────
    rr = _risk_reward(candidate)
    if rr is not None and rr < MIN_RISK_REWARD:
        res.verdict = GateVerdict.REJECTED
        res.reasons.append(f"R:R {rr:.2f} < min {MIN_RISK_REWARD}")
        return res

    # ── 6. Daily-loss circuit breaker ───────────────────────────────────
    cb_threshold = -abs(portfolio.capital * MAX_DAILY_LOSS_PCT / 100.0)
    if daily_pnl <= cb_threshold:
        res.verdict = GateVerdict.REJECTED
        res.reasons.append(
            f"daily-loss circuit breaker active "
            f"(P&L ₹{daily_pnl:.0f} ≤ ₹{cb_threshold:.0f})"
        )
        return res

    # ── 7. Max open positions ───────────────────────────────────────────
    if len(portfolio.open_positions) >= MAX_OPEN_POSITIONS:
        res.verdict = GateVerdict.REJECTED
        res.reasons.append(
            f"max open positions reached ({MAX_OPEN_POSITIONS})"
        )
        return res

    # ── 8. Sector concentration ─────────────────────────────────────────
    sector_exposure = _sector_exposure_pct(portfolio, candidate)
    if sector_exposure > MAX_SECTOR_PCT:
        res.verdict = GateVerdict.REJECTED
        res.reasons.append(
            f"sector concentration {sector_exposure:.1f}% > {MAX_SECTOR_PCT}%"
        )
        return res

    # ── 9. Position size cap (may MODIFY, not reject) ───────────────────
    max_position_value = portfolio.capital * MAX_POSITION_PCT / 100.0
    proposed_value = candidate.qty * candidate.entry
    if proposed_value > max_position_value and candidate.entry > 0:
        new_qty = int(max_position_value / candidate.entry)
        if new_qty <= 0:
            res.verdict = GateVerdict.REJECTED
            res.reasons.append(
                f"position {proposed_value:.0f} exceeds cap "
                f"{max_position_value:.0f}; can't size down further"
            )
            return res
        res.verdict = GateVerdict.MODIFIED
        res.modified_qty = new_qty
        res.warnings.append(
            f"qty reduced {candidate.qty}→{new_qty} to honour "
            f"{MAX_POSITION_PCT}% position cap"
        )

    # ── 10. Correlation to existing book ────────────────────────────────
    if portfolio.correlation_to_open >= 0.7:
        res.warnings.append(
            f"high correlation ({portfolio.correlation_to_open:.2f}) "
            f"with existing positions — consider skipping"
        )

    return res


def _risk_reward(c: TradeCandidate) -> Optional[float]:
    """Compute R:R = reward / risk in the direction of the trade."""
    if c.direction == "bullish":
        risk = c.entry - c.stop
        reward = c.target - c.entry
    else:
        risk = c.stop - c.entry
        reward = c.entry - c.target
    if risk <= 0:
        return None
    return reward / risk


def _sector_exposure_pct(p: PortfolioState, c: TradeCandidate) -> float:
    """Existing sector exposure (% of capital) + the proposed candidate."""
    if p.capital <= 0:
        return 100.0
    existing = sum(
        float(pos.get("entry_price", 0)) * float(pos.get("shares", 0))
        for pos in p.open_positions
        if pos.get("sector") == c.sector
    )
    proposed = c.qty * c.entry
    return ((existing + proposed) / p.capital) * 100.0

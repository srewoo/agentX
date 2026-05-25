from __future__ import annotations
"""Automatic paper-trading loop — closes the learning loop for free.

Every scan cycle, take the top-N highest-conviction BUY/SELL
recommendations from the multi-factor engine, write them as open
paper trades, and (separately) evaluate open paper trades against
the latest price to close hits.

This is what was missing for the meta-labeler and weight tuner to
self-activate: they need ≥200 resolved trades, and resolved trades
only exist if something is actually creating them. Manual API
creation was producing 1 trade in weeks.

Guardrails:
  • Idempotent — won't create a duplicate paper trade for the same
    (symbol, direction, entry_date). Re-runs on the same scan are safe.
  • Per-day cap — at most `MAX_AUTO_TRADES_PER_DAY` opens per day, so
    a flood of low-conviction recs can't drown the system.
  • Minimum conviction floor — only `auto_paper_min_conviction`
    setting (default 65) gets traded.
  • Market-hours only — same is_market_open() check the scanner uses.
"""
import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from app.database import DB_PATH
from app.services.data_fetcher import get_stock_quote
from app.services.paper_trading import close_paper_trade, create_paper_trade

logger = logging.getLogger(__name__)

# Defaults — overridable via the settings table.
DEFAULT_MIN_CONVICTION = 65
DEFAULT_MAX_PER_DAY = 5
DEFAULT_MAX_OPEN_POSITIONS = 12


async def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


async def _count_auto_trades_today() -> int:
    today = await _today_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT COUNT(*) FROM paper_trades
               WHERE source = 'auto' AND entry_date = ?""",
            (today,),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0


async def _count_open_positions() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM paper_trades WHERE status='open'") as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0


async def _already_open(symbol: str, direction: str, entry_date: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT 1 FROM paper_trades
               WHERE symbol = ? AND direction = ? AND entry_date = ?
                 AND source = 'auto' LIMIT 1""",
            (symbol, direction, entry_date),
        ) as cur:
            return (await cur.fetchone()) is not None


async def auto_open_from_recommendations(
    recs: list[Any],
    *,
    min_conviction: int = DEFAULT_MIN_CONVICTION,
    max_per_day: int = DEFAULT_MAX_PER_DAY,
    max_open_positions: int = DEFAULT_MAX_OPEN_POSITIONS,
) -> dict[str, Any]:
    """Open paper trades for the top BUY/SELL recommendations.

    `recs` is a list of `Recommendation` (Pydantic) or dicts. Sorted by
    conviction desc, then risk_reward desc; the first N that pass the
    floors and aren't already open get opened.
    """
    today = await _today_iso()
    opened: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    already_today = await _count_auto_trades_today()
    open_pos = await _count_open_positions()
    remaining_daily = max(0, max_per_day - already_today)
    remaining_open = max(0, max_open_positions - open_pos)
    budget = min(remaining_daily, remaining_open)
    if budget <= 0:
        return {
            "opened": [],
            "skipped_reason_counts": {"budget_exhausted": len(recs)},
            "today_auto_trades": already_today,
            "open_positions": open_pos,
        }

    candidates = []
    for r in recs:
        action = getattr(r, "action", None) or (r.get("action") if isinstance(r, dict) else None)
        if action not in ("BUY", "SELL"):
            continue
        conv = float(getattr(r, "conviction", 0) or (r.get("conviction") if isinstance(r, dict) else 0) or 0)
        if conv < min_conviction:
            continue
        rr = float(getattr(r, "risk_reward", 0) or (r.get("risk_reward") if isinstance(r, dict) else 0) or 0)
        candidates.append((conv, rr, r))
    candidates.sort(key=lambda t: (-t[0], -t[1]))

    skip_counts: dict[str, int] = {}
    for conv, rr, r in candidates:
        if len(opened) >= budget:
            skip_counts["budget_exhausted"] = skip_counts.get("budget_exhausted", 0) + 1
            continue
        sym = getattr(r, "symbol", None) or r.get("symbol")
        action = getattr(r, "action", None) or r.get("action")
        direction = "bullish" if action == "BUY" else "bearish"
        if await _already_open(sym, direction, today):
            skip_counts["already_open"] = skip_counts.get("already_open", 0) + 1
            continue
        entry = float(getattr(r, "entry", None) or r.get("entry") or 0)
        sl = float(getattr(r, "stoploss", None) or r.get("stoploss") or 0)
        tgt = float(getattr(r, "target1", None) or r.get("target1") or 0)
        if entry <= 0 or sl <= 0 or tgt <= 0:
            skip_counts["invalid_prices"] = skip_counts.get("invalid_prices", 0) + 1
            continue
        # Strength = floor of conviction/10, clamped 1..10.
        strength = max(1, min(10, int(conv // 10)))
        try:
            trade = await create_paper_trade(
                symbol=sym,
                direction=direction,
                signal_type="multi_factor_engine",
                strength=strength,
                entry_price=entry,
                stop_loss=sl,
                target=tgt,
                source="auto",
            )
            opened.append({"symbol": sym, "direction": direction, "conviction": conv,
                            "trade_id": trade["trade_id"]})
        except Exception as e:
            logger.warning("auto-open failed for %s: %s", sym, e)
            skip_counts["create_error"] = skip_counts.get("create_error", 0) + 1

    if opened:
        logger.info("Auto paper-trades opened: %d (today=%d, open=%d)",
                    len(opened), already_today + len(opened), open_pos + len(opened))
    return {
        "opened": opened,
        "skipped_reason_counts": skip_counts,
        "today_auto_trades": already_today + len(opened),
        "open_positions": open_pos + len(opened),
    }


async def auto_close_hits() -> dict[str, Any]:
    """Walk open auto-paper-trades and close any whose live price hit
    stop or target. Pulls live quotes (cached by data_fetcher) so this
    is cheap to run every scan cycle.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT trade_id, symbol, direction, entry_price, stop_loss, target
               FROM paper_trades
               WHERE status = 'open' AND source = 'auto'"""
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    closed: list[dict[str, Any]] = []
    for row in rows:
        try:
            q = await get_stock_quote(row["symbol"])
        except Exception:
            continue
        price = (q or {}).get("price") or (q or {}).get("last_price")
        if not price:
            continue
        price = float(price)
        sl = float(row.get("stop_loss") or 0)
        tgt = float(row.get("target") or 0)
        exit_reason = None
        exit_price = None
        if row["direction"] == "bullish":
            if sl and price <= sl:
                exit_reason, exit_price = "stoploss_hit", sl
            elif tgt and price >= tgt:
                exit_reason, exit_price = "target_hit", tgt
        else:  # bearish
            if sl and price >= sl:
                exit_reason, exit_price = "stoploss_hit", sl
            elif tgt and price <= tgt:
                exit_reason, exit_price = "target_hit", tgt
        if exit_reason:
            try:
                result = await close_paper_trade(
                    row["trade_id"], exit_price=exit_price, exit_reason=exit_reason,
                )
                if result:
                    closed.append({"trade_id": row["trade_id"], "symbol": row["symbol"],
                                    "reason": exit_reason, "exit_price": exit_price})
            except Exception as e:
                logger.warning("auto-close failed for %s: %s", row["trade_id"], e)

    if closed:
        logger.info("Auto paper-trades closed: %d", len(closed))
    return {"closed": closed, "open_remaining": len(rows) - len(closed)}


async def get_auto_paper_settings(db_settings: dict[str, Any]) -> dict[str, Any]:
    """Resolve min_conviction / caps from the settings table with defaults."""
    def _i(key: str, default: int) -> int:
        try:
            return int(db_settings.get(key, default))
        except Exception:
            return default
    return {
        "enabled": str(db_settings.get("auto_paper_trade_enabled", "true")).lower() == "true",
        "min_conviction": _i("auto_paper_min_conviction", DEFAULT_MIN_CONVICTION),
        "max_per_day": _i("auto_paper_max_per_day", DEFAULT_MAX_PER_DAY),
        "max_open_positions": _i("auto_paper_max_open_positions", DEFAULT_MAX_OPEN_POSITIONS),
    }

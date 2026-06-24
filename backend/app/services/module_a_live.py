from __future__ import annotations
"""Live-firing layer for Module A (Quality + Value + 52w-low).

Run during each market-hours scan cycle, alongside the multi-factor
engine's auto-paper-trader. The two streams write to `paper_trades`
with different `source` tags so we can separately track the
high-frequency multi-factor system's win rate vs the low-frequency
QV system's.

Module A's expected cadence: 1-3 picks per WEEK across the entire
universe, hold each for 180 trading days, exit only on time barrier
or -20% catastrophe stop. Far lower frequency than the multi-factor
swing engine — which is the WHOLE POINT.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

from app.services.data_fetcher import MAJOR_STOCKS, async_fetch_history, get_stock_quote
from app.services.quality_value_backtester import (
    _price_only_quality_proxy, _rolling_52w_low,
)
from app.services.quality_value_strategy import (
    QV_FILTERS, passes_qv_filters, qv_entry_targets,
)
from app.services.market_data import get_corporate_actions

logger = logging.getLogger(__name__)


# Tunable defaults — overridable via settings.
MODULE_A_DEFAULTS = {
    "enabled": True,
    "min_composite": 40,                # price-only floor
    "max_pct_above_52w_low": 25.0,
    "require_sma200_above": True,       # eliminates value traps in structural downtrends
    "earnings_blackout_days": 7,        # skip stocks with results within 7 days
    "max_picks_per_run": 3,
    "max_open_module_a_positions": 8,
    "fundamentals_mode": "price_only",  # auto-flips to deep if available
}


async def _evaluate_symbol(
    sym: str, *, filters: dict[str, Any], fundamentals_mode: str,
    corp_actions: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Return a candidate dict if `sym` currently passes QV filters."""
    df = await async_fetch_history(sym, period="2y", interval="1d")
    if df is None or df.empty or len(df) < 260:
        return None
    close = df["Close"]
    price = float(close.iloc[-1])
    if price <= 0:
        return None
    fl_series = _rolling_52w_low(close).values
    fl_low = float(fl_series[-1]) if fl_series[-1] == fl_series[-1] else 0.0
    volumes = df["Volume"].values
    adv = float(pd.Series(close.iloc[-20:]).mean() * pd.Series(volumes[-20:]).mean())

    sma200_val: Optional[float] = None
    sma200_series = close.rolling(200, min_periods=100).mean()
    if not pd.isna(sma200_series.iloc[-1]):
        sma200_val = float(sma200_series.iloc[-1])

    near_earnings = _check_earnings_blackout(sym, corp_actions,
                                              days=int(filters.get("earnings_blackout_days", 7)))

    composite = 0
    pe = sector_pe = roe = fcf = net_debt_to_ebitda = None
    sector = None
    if fundamentals_mode == "deep":
        try:
            from app.services.fundamentals_deep import get_deep_fundamentals
            from app.services.fundamentals import get_fundamentals
            deep = await get_deep_fundamentals(sym)
            if deep and not deep.get("error"):
                composite = int(deep.get("composite_score", 0))
                fcf = (deep.get("cash_flow") or {}).get("fcf")
                net_debt_to_ebitda = (deep.get("balance_sheet") or {}).get("net_debt_to_ebitda")
            leg = await get_fundamentals(sym)
            roe = (leg.get("profitability") or {}).get("roe")
            pe = (leg.get("valuation") or {}).get("pe")
            sector = (leg.get("sector") or "").strip() or None
        except Exception:
            pass
    if not composite:
        composite = int(_price_only_quality_proxy(close))

    passed, audit = passes_qv_filters(
        price=price, fiftytwo_week_low=fl_low, avg_daily_value_inr=adv,
        pe=pe, sector_pe_median=sector_pe, roe=roe,
        net_debt_to_ebitda=net_debt_to_ebitda, fcf=fcf,
        composite_score=composite, sector=sector,
        sma200=sma200_val, near_earnings=near_earnings,
        filters=filters,
    )
    if not passed:
        return None

    # 14-day ATR for sizing context (sl/t1/t2 below use catastrophe stop).
    high = df["High"].values; low = df["Low"].values
    tr = pd.Series([
        max(float(high[i]) - float(low[i]),
            abs(float(high[i]) - float(close.iloc[i - 1])) if i > 0 else 0,
            abs(float(low[i]) - float(close.iloc[i - 1])) if i > 0 else 0)
        for i in range(len(df))
    ])
    atr_14 = float(tr.rolling(14).mean().iloc[-1])
    entry, sl, t1, t2 = qv_entry_targets(price=price, atr=atr_14, filters=filters)

    return {
        "symbol": sym, "price": price, "fiftytwo_week_low": fl_low,
        "pct_above_low": (price - fl_low) / fl_low * 100 if fl_low else None,
        "composite_score": composite,
        "entry": entry, "stoploss": sl, "target1": t1, "target2": t2,
        "audit": audit,
    }


def _check_earnings_blackout(
    symbol: str,
    actions: list[dict[str, Any]],
    days: int = 7,
) -> bool:
    """True when an earnings/results event falls within `days` calendar days."""
    from datetime import datetime, timezone
    if not actions:
        return False
    sym_u = symbol.upper()
    today = datetime.now(timezone.utc).date()
    from datetime import timedelta
    horizon_end = today + timedelta(days=days)
    for a in actions:
        if (a.get("symbol") or "").upper() != sym_u:
            continue
        atype = (a.get("action_type") or a.get("subject") or "").lower()
        if "result" not in atype and "earning" not in atype:
            continue
        ex_str = a.get("ex_date") or a.get("date") or a.get("recordDate")
        if not ex_str:
            continue
        try:
            ex_date = datetime.fromisoformat(ex_str[:10]).date()
        except Exception:
            continue
        if today <= ex_date <= horizon_end:
            return True
    return False


async def _count_open_module_a() -> int:
    import aiosqlite
    from app.database import DB_PATH
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE status='open' AND source='module_a'"
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0


async def _already_open_module_a(symbol: str) -> bool:
    import aiosqlite
    from app.database import DB_PATH
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT 1 FROM paper_trades
               WHERE symbol = ? AND source = 'module_a' AND status = 'open' LIMIT 1""",
            (symbol,),
        ) as cur:
            return (await cur.fetchone()) is not None


def _build_module_a_signal(c: dict[str, Any], trade_id: str) -> dict[str, Any]:
    """Build a `signals`-table row for a Module A pick so it surfaces in the
    extension feed alongside the multi-factor engine's signals. Strength 10
    mirrors the paper trade; direction is always bullish (QV is long-only)."""
    pct = c.get("pct_above_low")
    pct_txt = f"{pct:.1f}% above 52w low" if pct is not None else "near 52w low"
    return {
        "id": uuid.uuid4().hex,
        "symbol": c["symbol"],
        "signal_type": "quality_value_52w_low",
        "direction": "bullish",
        "strength": 10,
        "reason": (
            f"Quality+Value pick: composite {c.get('composite_score', 0):.0f}, "
            f"{pct_txt}. 180-day hold (Module A)."
        ),
        "risk": "Long-horizon position; -20% catastrophe stop, 180-day time exit.",
        "current_price": c.get("entry"),
        "exchange": "NSE",
        "metadata": {
            "source": "module_a",
            "trade_id": trade_id,
            "composite_score": c.get("composite_score"),
            "pct_above_low": pct,
            "horizon_days": 180,
            "entry": c.get("entry"),
            "stop_loss": c.get("stoploss"),
            "target": c.get("target1"),
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


async def scan_and_open_module_a(
    db_settings: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Scan the universe for fresh QV picks; open paper trades for the
    top `max_picks_per_run`. Idempotent — won't double-open a symbol
    that already has an open Module A position.
    """
    from app.services.paper_trading import create_paper_trade

    cfg = {**MODULE_A_DEFAULTS, **(db_settings or {})}
    if not cfg.get("enabled", True):
        return {"status": "disabled"}

    filters = {
        **QV_FILTERS,
        "min_composite": int(cfg["min_composite"]),
        "max_pct_above_52w_low": float(cfg["max_pct_above_52w_low"]),
    }

    open_count = await _count_open_module_a()
    remaining = int(cfg["max_open_module_a_positions"]) - open_count
    if remaining <= 0:
        return {"status": "max_open_reached", "open": open_count}

    syms = [s["symbol"] for s in MAJOR_STOCKS if not s["symbol"].startswith("^")]

    # Fetch corporate actions once; share across all symbol evaluations.
    try:
        corp_actions: list[dict[str, Any]] = await get_corporate_actions() or []
    except Exception:
        corp_actions = []

    sem = asyncio.Semaphore(6)

    async def _one(s: str):
        async with sem:
            try:
                return await _evaluate_symbol(s, filters=filters,
                                              fundamentals_mode=cfg["fundamentals_mode"],
                                              corp_actions=corp_actions)
            except Exception:
                return None

    candidates_raw = await asyncio.gather(*(_one(s) for s in syms))
    candidates = [c for c in candidates_raw if c is not None]

    # Rank by composite (desc), then by proximity to 52w-low (asc).
    candidates.sort(key=lambda c: (-c["composite_score"], c["pct_above_low"] or 999))

    opened: list[dict[str, Any]] = []
    emitted_signals: list[dict[str, Any]] = []
    skipped_already_open = 0
    for c in candidates:
        if len(opened) >= min(remaining, int(cfg["max_picks_per_run"])):
            break
        if await _already_open_module_a(c["symbol"]):
            skipped_already_open += 1
            continue
        try:
            trade = await create_paper_trade(
                symbol=c["symbol"], direction="bullish",
                signal_type="quality_value_52w_low",
                strength=10,
                entry_price=c["entry"], stop_loss=c["stoploss"],
                target=c["target1"], source="module_a",
            )
            opened.append({
                "trade_id": trade["trade_id"], "symbol": c["symbol"],
                "composite": c["composite_score"], "pct_above_low": c["pct_above_low"],
                "entry": c["entry"], "stop_loss": c["stoploss"], "target": c["target1"],
            })
            emitted_signals.append(_build_module_a_signal(c, trade["trade_id"]))
        except Exception as e:
            logger.warning("Module A open failed for %s: %s", c["symbol"], e)

    # Surface Module A picks in the signals feed (the extension reads `signals`,
    # not `paper_trades`). Without this, the only long-term BUY strategy was
    # invisible in the UI. Best-effort: a feed-write failure must not roll back
    # an already-opened paper trade.
    if emitted_signals:
        try:
            from app.services.orchestrator import _store_signals
            await _store_signals(emitted_signals)
        except Exception as e:
            logger.warning("Module A: failed to emit %d feed signals: %s",
                           len(emitted_signals), e)

    if opened:
        logger.info("Module A: opened %d picks (open=%d, candidates=%d)",
                    len(opened), open_count + len(opened), len(candidates))

    return {
        "status": "ok",
        "candidates_qualified": len(candidates),
        "opened": opened,
        "skipped_already_open": skipped_already_open,
        "open_positions_after": open_count + len(opened),
    }


async def evaluate_and_close_module_a() -> dict[str, Any]:
    """Close open Module A positions hitting catastrophe stop or 180-day
    time barrier. Mirrors `auto_close_hits` but applies Module A's
    exit rules (no profit target — let winners run; only -20% stop or
    time expiry).
    """
    import aiosqlite
    from datetime import datetime, timezone
    from app.database import DB_PATH
    from app.services.paper_trading import close_paper_trade

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT trade_id, symbol, entry_price, stop_loss, entry_date
               FROM paper_trades
               WHERE status='open' AND source='module_a'"""
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    closed: list[dict[str, Any]] = []
    today = datetime.now(timezone.utc).date()
    HOLD_DAYS = 180

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
        exit_reason = None
        exit_price = None
        if sl and price <= sl:
            exit_reason, exit_price = "catastrophe_stop", sl
        else:
            try:
                entry_date = datetime.fromisoformat(row["entry_date"]).date()
                if (today - entry_date).days >= HOLD_DAYS:
                    exit_reason, exit_price = "time_expired", price
            except Exception:
                pass
        if exit_reason:
            try:
                r = await close_paper_trade(row["trade_id"], exit_price=exit_price,
                                             exit_reason=exit_reason)
                if r:
                    closed.append({"trade_id": row["trade_id"], "symbol": row["symbol"],
                                    "reason": exit_reason, "exit_price": exit_price})
            except Exception as e:
                logger.warning("Module A close failed for %s: %s", row["trade_id"], e)

    if closed:
        logger.info("Module A: closed %d positions", len(closed))
    return {"closed": closed, "open_remaining": len(rows) - len(closed)}

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
from typing import Any, Optional

import aiosqlite

from app.database import DB_PATH
from app.services.data_fetcher import get_stock_quote
from app.services.paper_trading import close_paper_trade, create_paper_trade

logger = logging.getLogger(__name__)

# Defaults — overridable via the settings table.
DEFAULT_MIN_CONVICTION = 65
# 1.1 — widened forward-test funnel. A 200+ name universe can supply enough
# candidates to sustain ≥25 trades/week; the book must be deep enough to hold
# them given ~7-day time exits (5-8 new/day × ~7-day hold ⇒ ~30-40 concurrent).
# Per-trade notional shrinks with the book (kelly_sizing.per_position_cap_pct)
# so total gross exposure is unchanged vs the old 12-position book.
DEFAULT_MAX_PER_DAY = 8
DEFAULT_MAX_OPEN_POSITIONS = 30

# Time-exit horizons (calendar days), derived from the signal_outcomes
# backtest hold periods: the validated short-fuse setups resolve in a
# 1-3 SESSION move (double_top median 1d / p90 3d; gaps 1d/2d; breakout
# 1d/2d; rsi_extreme/macd_divergence ~2d). Holding past that just gives
# volatility room to hit the stop — observed live as double_top shorts
# held ~2 weeks going 1-for-9. Values are calendar days (p90 trading days
# + a weekend buffer). `auto_close_hits` force-exits at market once held.
DEFAULT_MAX_HOLD_DAYS = 7
MAX_HOLD_DAYS: dict[str, int] = {
    "price_spike": 2, "volume_spike": 2,
    "gap_up": 3, "gap_down": 3,
    "breakout": 4, "consolidation_breakout": 4,
    "rsi_extreme": 4, "ema_crossover": 4,
    "double_top": 5, "double_bottom": 5,
    "head_and_shoulders": 5, "inverse_head_and_shoulders": 5,
    "macd_divergence": 5, "macd_crossover": 5,
}


def _max_hold_days(signal_type: str | None) -> int:
    return MAX_HOLD_DAYS.get(signal_type or "", DEFAULT_MAX_HOLD_DAYS)


def _days_held(entry_date: str | None, now: datetime) -> Optional[int]:
    """Calendar days between entry_date and now. Tolerates both
    'YYYY-MM-DD' (csv import) and full ISO timestamps (api/auto)."""
    if not entry_date:
        return None
    try:
        dt = datetime.fromisoformat(entry_date.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (now - dt).days)
    except Exception:
        return None

# Directional-concentration guardrail. The engine can lock into a one-sided
# posture (e.g. 100% short) and bleed when the market runs the other way —
# observed live as a 0-for-33 run of SELL recos into a rising tape. This caps
# how lopsided the OPEN book may get: once a direction holds >80% of an open
# book of at least N positions, no further same-direction entries are taken
# until the book rebalances (winners close / opposite-side entries appear).
# This protects capital without faking edge; it does not change what the
# engine *recommends*, only what the auto-trader is willing to *commit to*.
DEFAULT_MAX_DIRECTIONAL_CONCENTRATION = 0.80
DEFAULT_MIN_POSITIONS_FOR_CONCENTRATION = 3

# Conviction → win-probability mapping for Kelly sizing.
# Deliberately conservative: the walk-forward evidence shows even strong
# signals realise only ~53% WR, so conviction 100 maps to just ~0.57 and
# conviction 50 to a coin-flip. This keeps Kelly fractions honest — we never
# let the engine's self-reported confidence inflate the bet beyond what the
# measured edge supports. Combined with ¼-Kelly + caps, sizing stays sane.
_WIN_PROB_BASE = 0.50          # win prob at conviction 50
_WIN_PROB_SLOPE = 0.0014       # per conviction point above/below 50
_WIN_PROB_CAP = 0.58           # never imply more edge than the data shows


def _win_probability(conviction: float) -> float:
    """Map a 0-100 conviction to a conservative win probability for Kelly.

    Heavily shrunk toward 0.5 — see module-level rationale. A separately
    measured/calibrated probability (e.g. meta-label output) should be
    preferred by callers when available; this is the conviction-only floor.
    """
    p = _WIN_PROB_BASE + (float(conviction) - 50.0) * _WIN_PROB_SLOPE
    return max(0.35, min(_WIN_PROB_CAP, p))


def _effective_win_prob(conviction: float, meta_label_prob: Optional[float]) -> float:
    """Win probability to size Kelly on (B1) — ONE probability source, used once.

    When the recommendation carries a measured out-of-sample meta-label p(win),
    that IS the sizing probability, clamped to the same conservative bounds as
    the conviction map. The old ``min(conv_p, meta_p)`` double-counted a weak
    p(win): the meta gate upstream (recommendation.py) had already scaled
    conviction by p_meta, so the min shrank the bet by the same signal twice.
    Without a deployed meta model, the conviction-only heuristic applies.
    """
    if meta_label_prob is not None:
        # Cap (never imply more edge than measured) but NO floor — flooring a
        # weak measured p(win) upward would inflate the bet, the opposite of
        # conservative. A low p simply lets Kelly skip the trade.
        return min(_WIN_PROB_CAP, max(0.0, float(meta_label_prob)))
    return _win_probability(conviction)


async def _fetch_volatility(symbol: str) -> tuple[Optional[float], Optional[float]]:
    """Best-effort ``(atr_pct, adx)`` for the risk gate's ATR-chop rule.

    Returns ``(None, None)`` on any failure (offline, thin history, source
    cooldown) so the chop gate stays *inert* rather than blocking a trade on
    missing data. Real numbers when the data layer can serve history.
    """
    try:
        from app.services.data_fetcher import async_fetch_history
        from app.services.technicals import compute_technicals
        df = await async_fetch_history(symbol, period="6mo")
        if df is None or len(df) < 30:
            return None, None
        tech = compute_technicals(df)
        atr_pct = tech.get("atr_pct")
        adx = tech.get("adx")
        return (
            float(atr_pct) if atr_pct is not None else None,
            float(adx) if adx is not None else None,
        )
    except Exception:
        return None, None


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


async def _portfolio_risk_state(capital: float) -> tuple:
    """(vix, recent_loss_streak, peak_equity, current_equity) for B4/B5.

    All best-effort: VIX from market context (None on failure), the recent
    losing streak and the realized equity curve from closed paper trades.
    Equity = capital + cumulative realized PnL; peak is its running max.
    """
    vix = None
    try:
        from app.services.market_data import get_india_vix
        vix = await get_india_vix()
    except Exception:
        vix = None

    recent_losses = 0
    peak = capital
    current = capital
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT pnl_amount FROM paper_trades WHERE status='closed' "
                "AND pnl_amount IS NOT NULL ORDER BY exit_date ASC, trade_id ASC"
            ) as cur:
                rows = [r[0] for r in await cur.fetchall()]
        running = capital
        for pnl in rows:
            running += float(pnl or 0.0)
            peak = max(peak, running)
        current = running
        # Trailing losing streak (most recent closed trades).
        for pnl in reversed(rows):
            if float(pnl or 0.0) < 0:
                recent_losses += 1
            else:
                break
    except Exception:
        pass
    return vix, recent_losses, peak, current


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
    max_directional_concentration: float = DEFAULT_MAX_DIRECTIONAL_CONCENTRATION,
    min_positions_for_concentration: int = DEFAULT_MIN_POSITIONS_FOR_CONCENTRATION,
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

    # ── forward decision log (D1) ──
    # One snapshot per actionable (BUY/SELL) recommendation we evaluate —
    # taken AND skipped, with the reason — so selection bias is auditable.
    # HOLD/AVOID recs are never trade candidates and are intentionally not
    # row-logged (they'd bloat the table by the size of the universe).
    decisions: list[dict[str, Any]] = []

    def _record(r, conv, rr, *, taken, skip_reason=None, direction=None,
                sizing=None, max_corr=None) -> None:
        def g(k):
            v = getattr(r, k, None)
            if v is None and isinstance(r, dict):
                v = r.get(k)
            return v
        snap: dict[str, Any] = {
            "symbol": g("symbol"), "horizon": g("horizon"), "action": g("action"),
            "direction": direction, "conviction": int(conv),
            "meta_label_prob": g("meta_label_prob"),
            "entry": g("entry"), "stoploss": g("stoploss"), "target1": g("target1"),
            "risk_reward": rr, "regime": g("regime"), "sector": g("sector"),
            "weighted_score": g("weighted_score"), "factor_agreement": g("factor_agreement"),
            "taken": taken, "skip_reason": skip_reason, "max_correlation": max_corr,
            "trade_date": today, "source": "auto",
            "factors": [
                {"name": getattr(c, "name", None), "score": getattr(c, "score", None)}
                for c in (g("signals") or []) if not isinstance(c, dict)
            ] or g("signals"),
        }
        if sizing:
            snap.update({
                "win_prob_used": sizing.get("win_prob_used"),
                "kelly_f_used": sizing.get("kelly_f_used"),
                "payoff_ratio": sizing.get("payoff_ratio"),
                "shares": sizing.get("shares"),
                "position_value": sizing.get("position_value"),
                "binding_constraint": sizing.get("binding_constraint"),
            })
        decisions.append(snap)

    candidates = []
    for r in recs:
        action = getattr(r, "action", None) or (r.get("action") if isinstance(r, dict) else None)
        if action not in ("BUY", "SELL"):
            continue
        conv = float(getattr(r, "conviction", 0) or (r.get("conviction") if isinstance(r, dict) else 0) or 0)
        rr = float(getattr(r, "risk_reward", 0) or (r.get("risk_reward") if isinstance(r, dict) else 0) or 0)
        if conv < min_conviction:
            _record(r, conv, rr, taken=False, skip_reason="below_min_conviction",
                    direction=("bullish" if action == "BUY" else "bearish"))
            continue
        candidates.append((conv, rr, r))
    candidates.sort(key=lambda t: (-t[0], -t[1]))

    # Snapshot open positions ONCE for correlation + risk-gate evaluation —
    # the gate needs PortfolioState, and we don't want to hit the DB per
    # candidate inside the loop.
    open_positions_rows: list[dict] = []
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT symbol, direction, entry_price, position_size, shares "
                "FROM paper_trades WHERE status='open'"
            ) as cur:
                open_positions_rows = [dict(r) for r in await cur.fetchall()]
    except Exception:
        open_positions_rows = []

    # ── 2.2: delegate the entry decision to the pure decision core ──
    # All IO (already-open, correlation, ATR/ADX, earnings) is resolved here,
    # then decide() applies the SAME rules the backtest uses. One decision
    # function, live and backtest — no drift.
    from app.services.decision_core import (
        EntryCandidate, PortfolioCtx, DecisionConfig, decide,
    )

    # Capital floor for sizing/gate: best-effort from settings, fallback 100k.
    capital = 100_000.0
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT value FROM settings WHERE key='paper_capital'"
            ) as cur:
                row = await cur.fetchone()
                if row and row[0]:
                    capital = float(row[0])
    except Exception:
        pass

    # Risk-gate availability decides whether decide() runs the 10-rule gate.
    try:
        import app.services.risk_gate as _rg  # noqa: F401
        _risk_gate_available = True
    except Exception:
        _risk_gate_available = False

    try:
        from app.services.portfolio_correlation import correlation_to_open
        _corr_available = True
    except Exception:
        _corr_available = False

    # Directional tally + gross/net exposure seeded from the current open book.
    dir_counts: dict[str, int] = {"bullish": 0, "bearish": 0}
    sector_open: dict[str, float] = {}
    gross_open = 0.0
    net_open = 0.0
    for p in open_positions_rows:
        d = p.get("direction")
        if d in dir_counts:
            dir_counts[d] += 1
        val = float(p.get("position_size") or 0.0)
        gross_open += val
        net_open += val if d == "bullish" else -val

    # B4/B5 portfolio risk state (VIX, recent-loss streak, equity peak/current).
    vix = None
    recent_losses = 0
    peak_eq = cur_eq = None
    try:
        vix, recent_losses, peak_eq, cur_eq = await _portfolio_risk_state(capital)
    except Exception as e:
        logger.debug("portfolio risk state skipped: %s", e)

    # ── Resolve per-candidate IO, then build the pure decision inputs. ──
    open_syms_all = [p["symbol"] for p in open_positions_rows if p.get("symbol")]
    entry_cands: list[EntryCandidate] = []
    cand_meta: dict[tuple, tuple] = {}
    for conv, rr, r in candidates:
        sym = getattr(r, "symbol", None) or r.get("symbol")
        action = getattr(r, "action", None) or r.get("action")
        direction = "bullish" if action == "BUY" else "bearish"
        entry = float(getattr(r, "entry", None) or r.get("entry") or 0)
        sl = float(getattr(r, "stoploss", None) or r.get("stoploss") or 0)
        tgt = float(getattr(r, "target1", None) or r.get("target1") or 0)
        meta_p = getattr(r, "meta_label_prob", None)
        if meta_p is None and isinstance(r, dict):
            meta_p = r.get("meta_label_prob")
        win_prob = _effective_win_prob(conv, meta_p)
        already = await _already_open(sym, direction, today)

        max_corr = 0.0
        if _corr_available and open_syms_all:
            try:
                open_syms = [s for s in open_syms_all if s != sym]
                if open_syms:
                    corr = await correlation_to_open(sym, open_syms)
                    if isinstance(corr, dict):
                        max_corr = float(corr.get("max_correlation") or 0.0)
                    elif isinstance(corr, (int, float)):
                        max_corr = float(corr)
            except Exception:
                max_corr = 0.0

        atr_pct, adx = await _fetch_volatility(sym)
        earnings_blackout = False
        try:
            from app.services.fmp_fetcher import is_in_earnings_blackout
            bl = await is_in_earnings_blackout(sym)
            earnings_blackout = bool(bl) if bl is not None else False
        except Exception as e:
            logger.debug("earnings-blackout check skipped for %s: %s", sym, e)

        data_quality = (
            getattr(r, "data_quality", None)
            or (r.get("data_quality") if isinstance(r, dict) else None)
        )
        sector = str(getattr(r, "sector", "") or (r.get("sector") if isinstance(r, dict) else "") or "Unknown")

        cand_meta[(sym, direction)] = (r, conv, rr)
        entry_cands.append(EntryCandidate(
            symbol=sym, direction=direction, conviction=conv, risk_reward=rr,
            entry=entry, stop=sl, target=tgt, sector=sector, win_prob=win_prob,
            already_open=already, max_correlation=max_corr, atr_pct=atr_pct,
            adx=adx, data_quality=data_quality, earnings_blackout=earnings_blackout))

    ctx = PortfolioCtx(
        capital=capital, already_today=already_today, open_count=open_pos,
        dir_counts=dir_counts, sector_open=sector_open, gross_open=gross_open,
        net_open=net_open, vix=vix, recent_losses=recent_losses or 0,
        peak_equity=peak_eq, cur_equity=cur_eq, open_positions=open_positions_rows)
    cfg = DecisionConfig(
        min_conviction=min_conviction, max_per_day=max_per_day,
        max_open_positions=max_open_positions,
        max_directional_concentration=max_directional_concentration,
        min_positions_for_concentration=min_positions_for_concentration,
        enable_risk_gate=_risk_gate_available)

    result = decide(entry_cands, ctx, cfg)
    skip_counts: dict[str, int] = dict(result.skip_reason_counts)

    # ── Drawdown breaker halted all entries. ──
    if result.drawdown_tripped:
        logger.warning("Drawdown breaker tripped (%.1f%%) — no new auto entries",
                       result.drawdown_pct)
        for s in result.skipped:
            r, conv, rr = cand_meta.get((s.symbol, s.direction), (None, 0, 0))
            if r is not None:
                _record(r, conv, rr, taken=False, skip_reason=s.reason, direction=s.direction)
        try:
            from app.services.decision_log import log_decisions
            await log_decisions(decisions, db_path=DB_PATH)
        except Exception as e:
            logger.debug("decision_log persistence skipped: %s", e)
        return {
            "opened": [],
            "skipped_reason_counts": {"drawdown_breaker": len(candidates)},
            "today_auto_trades": already_today,
            "open_positions": open_pos,
            "drawdown_pct": result.drawdown_pct,
            "decisions_logged": len(decisions),
        }

    # ── Execute the orders decide() returned. ──
    for o in result.orders:
        r, conv, rr = cand_meta[(o.symbol, o.direction)]
        try:
            trade = await create_paper_trade(
                symbol=o.symbol, direction=o.direction,
                signal_type="multi_factor_engine", strength=o.strength,
                entry_price=o.entry, stop_loss=o.stop, target=o.target,
                position_size=o.position_size, shares=o.shares, source="auto")
            opened.append({"symbol": o.symbol, "direction": o.direction,
                           "conviction": conv, "trade_id": trade["trade_id"],
                           "shares": o.shares, "position_size": o.position_size})
            _record(r, conv, rr, taken=True, direction=o.direction,
                    sizing=o.sizing, max_corr=o.max_correlation)
        except Exception as e:
            logger.warning("auto-open failed for %s: %s", o.symbol, e)
            skip_counts["create_error"] = skip_counts.get("create_error", 0) + 1
            _record(r, conv, rr, taken=False, skip_reason="create_error",
                    direction=o.direction, sizing=o.sizing, max_corr=o.max_correlation)

    # ── Log the skips (taken=False) with their reasons. ──
    for s in result.skipped:
        r, conv, rr = cand_meta[(s.symbol, s.direction)]
        _record(r, conv, rr, taken=False, skip_reason=s.reason,
                direction=s.direction, sizing=s.sizing, max_corr=s.max_correlation)
        # 4.3 — log a random ~5% shadow sample of REJECTED candidates so the
        # funnel's discard quality (selection bias) is measurable. Never trades
        # real capital; outcomes simulated later. Fire-and-forget.
        try:
            from app.services.shadow_sample import log_shadow_reject
            c = next((ec for ec in entry_cands
                      if ec.symbol == s.symbol and ec.direction == s.direction), None)
            if c is not None:
                await log_shadow_reject(
                    symbol=c.symbol, direction=c.direction, entry=c.entry,
                    stop=c.stop, target=c.target, reason=s.reason)
        except Exception as e:
            logger.debug("shadow-reject log skipped: %s", e)

    if opened:
        logger.info("Auto paper-trades opened: %d (today=%d, open=%d)",
                    len(opened), already_today + len(opened), open_pos + len(opened))

    # Persist the forward decision log (D1) — fire-and-forget, never blocks or
    # breaks the trade path. One row per actionable candidate, taken or not.
    try:
        from app.services.decision_log import log_decisions
        await log_decisions(decisions, db_path=DB_PATH)
    except Exception as e:
        logger.debug("decision_log persistence skipped: %s", e)

    return {
        "opened": opened,
        "skipped_reason_counts": skip_counts,
        "today_auto_trades": already_today + len(opened),
        "open_positions": open_pos + len(opened),
        "decisions_logged": len(decisions),
    }


async def auto_close_hits() -> dict[str, Any]:
    """Walk open auto-paper-trades and close any whose live price hit
    stop or target. Pulls live quotes (cached by data_fetcher) so this
    is cheap to run every scan cycle.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT trade_id, symbol, direction, entry_price, stop_loss, target,
                      signal_type, entry_date
               FROM paper_trades
               WHERE status = 'open' AND source = 'auto'"""
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    now = datetime.now(timezone.utc)
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
        # Time-exit: the signal's edge is a short-fuse move (see MAX_HOLD_DAYS).
        # If neither stop nor target hit within the horizon, exit at market —
        # don't let a 1-3 day edge bleed out over weeks.
        if not exit_reason:
            held = _days_held(row.get("entry_date"), now)
            if held is not None and held >= _max_hold_days(row.get("signal_type")):
                exit_reason, exit_price = "time_exit", price
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

    def _f(key: str, default: float) -> float:
        try:
            return float(db_settings.get(key, default))
        except Exception:
            return default
    return {
        "enabled": str(db_settings.get("auto_paper_trade_enabled", "true")).lower() == "true",
        "min_conviction": _i("auto_paper_min_conviction", DEFAULT_MIN_CONVICTION),
        "max_per_day": _i("auto_paper_max_per_day", DEFAULT_MAX_PER_DAY),
        "max_open_positions": _i("auto_paper_max_open_positions", DEFAULT_MAX_OPEN_POSITIONS),
        "max_directional_concentration": _f(
            "auto_paper_max_directional_concentration",
            DEFAULT_MAX_DIRECTIONAL_CONCENTRATION),
        "min_positions_for_concentration": _i(
            "auto_paper_min_positions_for_concentration",
            DEFAULT_MIN_POSITIONS_FOR_CONCENTRATION),
    }

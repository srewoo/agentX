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
DEFAULT_MAX_PER_DAY = 5
DEFAULT_MAX_OPEN_POSITIONS = 12

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
    """Win probability to size Kelly on (B1).

    The conviction map (`_win_probability`) is a hand-tuned heuristic. When the
    recommendation carries an out-of-sample meta-label probability — a measured
    p(win) from the trained secondary classifier — we bet off the **more
    conservative** of the two. Taking the min means a weak measured edge always
    shrinks the bet (and can drop it to zero via Kelly), while a strong
    measured edge never *inflates* it beyond the conviction-only ceiling. With
    no meta-label deployed yet, this is exactly the old behaviour.
    """
    conv_p = _win_probability(conviction)
    if meta_label_prob is None:
        return conv_p
    return min(conv_p, max(0.0, min(1.0, float(meta_label_prob))))


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

    # ── directional-concentration tally ──────────────────────────────────
    # Seed from the current open book, then keep it current as we open within
    # this cycle so the cap is cumulative across multiple opens (mirrors the
    # B3 exposure-cap bookkeeping below).
    dir_counts: dict[str, int] = {"bullish": 0, "bearish": 0}
    for p in open_positions_rows:
        d = p.get("direction")
        if d in dir_counts:
            dir_counts[d] += 1

    def _would_breach_concentration(direction: str) -> bool:
        """True if opening one more `direction` position would push that side
        above the concentration cap on a book of at least the floor size."""
        if max_directional_concentration >= 1.0:
            return False  # cap disabled
        proj_dir = dir_counts.get(direction, 0) + 1
        proj_total = dir_counts["bullish"] + dir_counts["bearish"] + 1
        if proj_total < max(1, min_positions_for_concentration):
            return False  # too few positions for the cap to be meaningful
        return (proj_dir / proj_total) > max_directional_concentration

    # Capital floor for the risk gate: best-effort from settings, fallback 100k.
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

    # Lazy-import these so missing modules don't break the existing path.
    try:
        from app.services.risk_gate import (
            TradeCandidate, PortfolioState, evaluate_trade, GateVerdict,
        )
        _risk_gate_available = True
    except Exception:
        _risk_gate_available = False

    try:
        from app.services.portfolio_correlation import correlation_to_open
        _corr_available = True
    except Exception:
        _corr_available = False

    try:
        from app.services.kelly_sizing import kelly_position_size
        _kelly_available = True
    except Exception:
        _kelly_available = False

    # ── B3/B4/B5 portfolio-aware sizing inputs (computed once per cycle) ──
    try:
        from app.services.portfolio_sizing import (
            apply_exposure_caps, correlation_size_multiplier,
            dynamic_kelly_fraction, drawdown_breaker_tripped,
        )
        _psizing_available = True
    except Exception:
        _psizing_available = False

    # B4: shrink the Kelly fraction in high-VIX regimes / after a losing streak.
    from app.services.kelly_sizing import DEFAULT_KELLY_FRACTION
    dyn_fraction = DEFAULT_KELLY_FRACTION
    # B3: aggregate the open book for sector + gross/net exposure caps.
    sector_open: dict[str, float] = {}
    gross_open = 0.0
    net_open = 0.0
    for p in open_positions_rows:
        val = float(p.get("position_size") or 0.0)
        gross_open += val
        net_open += val if (p.get("direction") == "bullish") else -val
    if _psizing_available:
        try:
            vix, recent_losses, peak_eq, cur_eq = await _portfolio_risk_state(capital)
            dyn_fraction = dynamic_kelly_fraction(
                DEFAULT_KELLY_FRACTION, vix=vix, recent_losses=recent_losses)
            # B5: halt all new entries while in a deep drawdown.
            breaker = drawdown_breaker_tripped(peak_eq, cur_eq)
            if breaker["tripped"]:
                logger.warning("Drawdown breaker tripped (%.1f%%) — no new auto entries",
                               breaker["drawdown_pct"])
                return {
                    "opened": [],
                    "skipped_reason_counts": {"drawdown_breaker": len(candidates)},
                    "today_auto_trades": already_today,
                    "open_positions": open_pos,
                    "drawdown_pct": breaker["drawdown_pct"],
                    "decisions_logged": 0,
                }
        except Exception as e:
            logger.debug("portfolio risk state skipped: %s", e)

    skip_counts: dict[str, int] = {}
    for conv, rr, r in candidates:
        if len(opened) >= budget:
            skip_counts["budget_exhausted"] = skip_counts.get("budget_exhausted", 0) + 1
            _record(r, conv, rr, taken=False, skip_reason="budget_exhausted")
            continue
        sym = getattr(r, "symbol", None) or r.get("symbol")
        action = getattr(r, "action", None) or r.get("action")
        direction = "bullish" if action == "BUY" else "bearish"
        if await _already_open(sym, direction, today):
            skip_counts["already_open"] = skip_counts.get("already_open", 0) + 1
            _record(r, conv, rr, taken=False, skip_reason="already_open", direction=direction)
            continue
        # ── directional-concentration guardrail ──
        # Refuse to add to an already-lopsided book. Candidates are sorted by
        # conviction desc, so the strongest same-direction names are kept and
        # only the marginal over-concentrating ones are dropped.
        if _would_breach_concentration(direction):
            skip_counts["directional_concentration_cap"] = (
                skip_counts.get("directional_concentration_cap", 0) + 1
            )
            logger.info(
                "SKIP auto-open %s: directional concentration cap "
                "(%s book would exceed %.0f%%; open bullish=%d bearish=%d)",
                sym, direction, max_directional_concentration * 100.0,
                dir_counts["bullish"], dir_counts["bearish"],
            )
            _record(r, conv, rr, taken=False,
                    skip_reason="directional_concentration_cap", direction=direction)
            continue
        entry = float(getattr(r, "entry", None) or r.get("entry") or 0)
        sl = float(getattr(r, "stoploss", None) or r.get("stoploss") or 0)
        tgt = float(getattr(r, "target1", None) or r.get("target1") or 0)
        if entry <= 0 or sl <= 0 or tgt <= 0:
            skip_counts["invalid_prices"] = skip_counts.get("invalid_prices", 0) + 1
            _record(r, conv, rr, taken=False, skip_reason="invalid_prices", direction=direction)
            continue
        # Strength = floor of conviction/10, clamped 1..10.
        strength = max(1, min(10, int(conv // 10)))

        # ── edge-aware Kelly sizing (the filter) ──
        # Size the position to its measured edge. A non-positive Kelly
        # fraction means the trade has no positive expectancy at this
        # win-probability + reward:risk — we skip it entirely. This is the
        # deterministic stand-in for the oracle that only bets positive-edge
        # trades (see kelly_sizing.py / backtest_results/CEILING_ANALYSIS.md).
        kelly_shares: int | None = None
        position_size: float | None = None
        sizing: dict[str, Any] | None = None
        if _kelly_available:
            try:
                meta_p = getattr(r, "meta_label_prob", None)
                if meta_p is None and isinstance(r, dict):
                    meta_p = r.get("meta_label_prob")
                sizing = kelly_position_size(
                    capital=capital, entry=entry, stop=sl, target=tgt,
                    win_prob=_effective_win_prob(conv, meta_p), direction=direction,
                    kelly_fraction_mult=dyn_fraction,  # B4: regime/streak-adjusted
                )
                if sizing["skip"]:
                    skip_counts["negative_kelly_edge"] = skip_counts.get("negative_kelly_edge", 0) + 1
                    logger.info(
                        "SKIP auto-open %s: %s (b=%.2f)",
                        sym, sizing["reason"], sizing["payoff_ratio"],
                    )
                    _record(r, conv, rr, taken=False, skip_reason="negative_kelly_edge",
                            direction=direction, sizing=sizing)
                    continue
                kelly_shares = int(sizing["shares"])
                position_size = float(sizing["position_value"])
            except Exception as e:
                logger.debug("kelly sizing skipped for %s: %s", sym, e)

        # ── pre-trade correlation check (≥ 0.7 to most-correlated open) ──
        max_corr = 0.0
        if _corr_available and open_positions_rows:
            try:
                open_syms = [p["symbol"] for p in open_positions_rows if p.get("symbol") and p["symbol"] != sym]
                if open_syms:
                    corr = await correlation_to_open(sym, open_syms)
                    if isinstance(corr, dict):
                        max_corr = float(corr.get("max_correlation") or 0.0)
                    elif isinstance(corr, (int, float)):
                        max_corr = float(corr)
            except Exception:
                max_corr = 0.0
        if max_corr >= 0.7:
            skip_counts["correlated_to_open"] = skip_counts.get("correlated_to_open", 0) + 1
            logger.info("REJECTED auto-open %s: corr %.2f >= 0.70 against open book", sym, max_corr)
            _record(r, conv, rr, taken=False, skip_reason="correlated_to_open",
                    direction=direction, sizing=sizing, max_corr=max_corr)
            continue

        # ── B2: graduated correlation trim (0.5 ≤ corr < 0.7 shrinks the bet) ──
        # ── B3: sector + gross/net exposure caps ──
        if _psizing_available and kelly_shares and position_size:
            corr_mult = correlation_size_multiplier(max_corr)
            capped = apply_exposure_caps(
                position_size * corr_mult, direction, capital=capital,
                sector=str(getattr(r, "sector", "") or (r.get("sector") if isinstance(r, dict) else "") or "Unknown"),
                sector_value_open=sector_open.get(
                    str(getattr(r, "sector", "") or (r.get("sector") if isinstance(r, dict) else "") or "Unknown"), 0.0),
                gross_open=gross_open, net_open=net_open,
            )
            allowed_value = capped["allowed_value"]
            new_shares = int(allowed_value / entry) if entry > 0 else 0
            if new_shares <= 0:
                skip_counts["exposure_capped"] = skip_counts.get("exposure_capped", 0) + 1
                logger.info("SKIP auto-open %s: exposure cap (%s) left no room", sym, capped["binding"])
                _record(r, conv, rr, taken=False,
                        skip_reason=f"exposure_capped:{capped['binding']}",
                        direction=direction, sizing=sizing, max_corr=max_corr)
                continue
            if new_shares < kelly_shares:
                kelly_shares = new_shares
                position_size = round(kelly_shares * entry, 2)

        # ── hard 10-rule risk gate ──
        if _risk_gate_available:
            try:
                # Use the Kelly-sized share count as the gate's qty so the
                # position-size / sector caps evaluate the size we actually
                # intend to take (falls back to 1 when Kelly is unavailable).
                gate_qty = kelly_shares if kelly_shares and kelly_shares > 0 else max(
                    1, int(getattr(r, "shares", 0) or r.get("shares") or 1)
                )
                # Quality-gate inputs: data_quality off the recommendation
                # (real), ATR%/ADX from a best-effort technicals fetch. Both
                # gates stay inert when their inputs aren't available.
                data_quality = (
                    getattr(r, "data_quality", None)
                    or (r.get("data_quality") if isinstance(r, dict) else None)
                )
                atr_pct, adx = await _fetch_volatility(sym)
                # Earnings blackout via FMP calendar — None (no key/failed)
                # leaves the gate inert; True triggers rejection in the gate.
                earnings_blackout = False
                try:
                    from app.services.fmp_fetcher import is_in_earnings_blackout
                    bl = await is_in_earnings_blackout(sym)
                    earnings_blackout = bool(bl) if bl is not None else False
                except Exception as e:
                    logger.debug("earnings-blackout check skipped for %s: %s", sym, e)
                candidate = TradeCandidate(
                    symbol=sym,
                    direction=direction,
                    entry=entry,
                    stop=sl,
                    target=tgt,
                    qty=gate_qty,
                    sector=str(getattr(r, "sector", "") or r.get("sector") or "Unknown"),
                    data_quality=data_quality,
                    atr_pct=atr_pct,
                    adx=adx,
                    is_in_earnings_blackout=earnings_blackout,
                )
                portfolio = PortfolioState(
                    capital=capital,
                    open_positions=open_positions_rows,
                    correlation_to_open=max_corr,
                )
                gate = evaluate_trade(candidate, portfolio)
                if gate.verdict == GateVerdict.REJECTED:
                    skip_counts["risk_gate_rejected"] = skip_counts.get("risk_gate_rejected", 0) + 1
                    logger.info("REJECTED auto-open %s by risk_gate: %s", sym, "; ".join(gate.reasons))
                    _record(r, conv, rr, taken=False,
                            skip_reason="risk_gate_rejected: " + "; ".join(gate.reasons),
                            direction=direction, sizing=sizing, max_corr=max_corr)
                    continue
                if gate.verdict == GateVerdict.MODIFIED and gate.modified_qty is not None:
                    # The gate trimmed our size — clamp Kelly shares to it.
                    if kelly_shares is not None:
                        kelly_shares = max(0, min(kelly_shares, gate.modified_qty))
                        if kelly_shares <= 0:
                            skip_counts["risk_gate_rejected"] = skip_counts.get("risk_gate_rejected", 0) + 1
                            logger.info("REJECTED auto-open %s: risk gate trimmed size to 0", sym)
                            _record(r, conv, rr, taken=False,
                                    skip_reason="risk_gate_trimmed_to_zero",
                                    direction=direction, sizing=sizing, max_corr=max_corr)
                            continue
                        position_size = round(kelly_shares * entry, 2)
                    strength = max(1, min(strength, gate.modified_qty))
            except Exception as e:
                logger.debug("risk_gate evaluation skipped for %s: %s", sym, e)

        try:
            trade = await create_paper_trade(
                symbol=sym,
                direction=direction,
                signal_type="multi_factor_engine",
                strength=strength,
                entry_price=entry,
                stop_loss=sl,
                target=tgt,
                position_size=position_size,
                shares=kelly_shares,
                source="auto",
            )
            opened.append({"symbol": sym, "direction": direction, "conviction": conv,
                            "trade_id": trade["trade_id"], "shares": kelly_shares,
                            "position_size": position_size})
            # Keep the directional tally current so the concentration cap is
            # cumulative across opens within this cycle.
            if direction in dir_counts:
                dir_counts[direction] += 1
            _record(r, conv, rr, taken=True, direction=direction,
                    sizing=sizing, max_corr=max_corr)
            # Keep the open-book aggregates current so B3 caps are cumulative
            # across multiple opens in the same cycle.
            if position_size:
                gross_open += position_size
                net_open += position_size if direction == "bullish" else -position_size
                _sec = str(getattr(r, "sector", "") or (r.get("sector") if isinstance(r, dict) else "") or "Unknown")
                sector_open[_sec] = sector_open.get(_sec, 0.0) + position_size
        except Exception as e:
            logger.warning("auto-open failed for %s: %s", sym, e)
            skip_counts["create_error"] = skip_counts.get("create_error", 0) + 1
            _record(r, conv, rr, taken=False, skip_reason="create_error",
                    direction=direction, sizing=sizing, max_corr=max_corr)

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

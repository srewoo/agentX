# agentX — One Pager

**Owner:** Founder · **Status:** Pre-launch (Chrome MV3 extension v1.1.0 + FastAPI backend) · **Last updated:** 2026-05-08

## Problem
Indian retail investors run a 5-tab research workflow: moneycontrol for news, screener.in for fundamentals, tickertape for scoring, tradingview for charts, Kite/Groww for execution. None of these talk to each other. The retail F&O trader (1.4 Cr active NSE clients, ~89% lose money per SEBI's 2024 study) gets no synthesised signal — they get raw data dumps and content marketing dressed as advice. Recommendation tools (Stockedge, Tickertape Pro) live in their own walled garden; the user has to context-switch *to* them. By the time a swing trader stitches FII flows + OI buildup + delivery% + RSI on RELIANCE, the move is gone.

## Target user
**Primary (beachhead):** F&O + swing-trading retail in tier-1 cities, ages 25–40, ticket size ₹50K–₹10L, already using 2+ research sites daily, active on fintwit and 1–2 Telegram tip channels they don't fully trust.
**Secondary:** Long-term equity investors who want explainable BUY/SELL conviction overlaid on screener.in.

## Insight
The research surface is already won — moneycontrol, screener.in, tickertape, tradingview own those eyeballs and won't be displaced. The interesting wedge isn't a new destination; it's a **lens that follows the user across their existing tabs**. agentX is a Chrome extension that overlays a multi-factor recommendation, OI/FII context, and portfolio-aware risk *on the page the user is already on*. Backend (`backend/app/services/recommendation.py`) blends 8 weighted factors (trend 20%, momentum 15%, volume/delivery 15%, F&O OI 15%, FII/DII 10%, relative strength 10%, sentiment 10%, vol 5%) into a 0–100 conviction with ATR-based entry/SL/targets per horizon — intraday, swing, positional.

## What we ship
1. **Chrome extension** (MV3) with content scripts on the 11 sites listed in `extension/manifest.json` — overlays a "agentX score" badge on every ticker the user encounters.
2. **Six-tab popup** (`extension/src/popup/App.tsx`): Live, Signals, Watchlist, Portfolio, Alerts, Settings — INR formatting, NSE/BSE toggle, dark theme.
3. **Backend** (FastAPI, 66% covered, 594 tests green): multi-factor recommendation, portfolio analytics (Sharpe, drawdown, beta, FIFO P&L — `backend/app/services/portfolio.py`), live WebSocket quotes (`/api/stream/quotes`), multi-channel alerts (Telegram/email/WhatsApp/SMS), backtester, LLM cost tracking.
4. **Explainability:** every recommendation ships its factor contributions — no black-box scores.

## Why now
- F&O retail volume has 6×'d since 2020; SEBI is tightening rules but demand hasn't cooled.
- LLM cost has dropped enough to run summarisation per ticker (`llm_analyst.py` already wired with cost tracking).
- Broker APIs are real now: Kite Connect, Upstox, Groww, Angel One all have OAuth — portfolio import is a 4-week build, not a 6-month one.
- Chrome MV3 is stable; the extension distribution channel is wide open vs gated mobile App Stores.

## Why us
- We're the only product that **overlays** rather than *replaces*. Tickertape and Stockedge want you on their site; we want you on yours.
- Engine is already shipped: 8-factor scoring, ATR bands, multi-horizon caching (`recommendation.py`).
- Backend is event-driven and modular — adding F&O OI heatmap, broker OAuth, or NLP screener is incremental, not a rewrite.

## Wedge vs incumbents
| Player | Wedge against them |
|---|---|
| **Tickertape** | Their score is opaque; ours is decomposed into 8 factors with weights you can see. We work *on their site*. |
| **Stockedge** | Mobile-first, alerts-heavy, but no explainability and weak F&O. We'll out-do them on F&O OI + Indian event calendar. |
| **Smallcase** | Curated baskets, not signals — different game. We integrate, not compete. |
| **Trendlyne** | Dense data, weak UX. We're the synthesised view on top. |
| **Sensibull** | Best-in-class options analytics — but standalone. We'll license/embed where possible, beat on equities. |
| **Streak** | Algo backtesting for retail; high learning curve. We give signals without making the user write strategies. |

## Success metrics
- **North star:** Weekly Active Tickers Researched (WATR) per user — the count of distinct symbols a user views agentX context on, per week. Target: 25 by week-4 of activation.
- **Supporting:**
  1. D30 retention ≥ 35% (Tickertape's public benchmark for paid tools is ~25%).
  2. Free → Pro conversion ≥ 4% by month-3.
  3. Telegram alert acknowledgement rate ≥ 60% (proxy for signal trust).

## Risks
1. **SEBI Research Analyst registration** — we cannot publish "BUY/SELL" calls without RA registration. Mitigation: frame as "signals + analytics", route through licensed RA partner, or get registered (≈₹1L + NISM XV-A exam — see `SEBI_COMPLIANCE.md`).
2. **Data licensing** — NSE/BSE charge for tick data; yfinance/scraped feeds are a launch hack, not a moat. Allocate ₹3–5L/yr to a licensed feed (Truedata, GlobalDatafeeds) before scaling.
3. **Trust in tip culture** — the same audience has been burned by Telegram tip channels. We must over-index on transparency: show the 8 factor weights, log every call.
4. **Chrome dependency** — Manifest V3 churn, single-browser distribution. Plan a mobile-web companion in 6 months.

## Open questions
- Pricing anchor: ₹999/yr (Tickertape) or ₹3,500/yr (Sensibull)? Lean Tickertape, gate F&O behind Elite tier.
- Do we partner with a registered RA from day one or self-register? Self-registration is a ~3-month process — partnering is the faster path to launch.
- Build tax harvesting (LTCG/STCG) ourselves or partner with Quicko/ClearTax? Probably partner.

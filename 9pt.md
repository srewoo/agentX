# agentX → Best-in-class: 9-Point Improvement Roadmap

A grounded list, ordered by ROI, of what stands between agentX today and a
genuinely *best-in-class* LLM-powered stock recommendation system for the
Indian market.

Each section follows the same shape:
- **Why this matters** — the gap it closes
- **Concrete actions** — specific files / hooks / changes
- **Effort** — trivial / moderate / heavy / multi-week
- **Done when** — measurable success criteria so we know it landed

> Last refreshed: 2026-05-26 after Tier 1+2 buildout + LLM layer prompts.

---

## 1. Evidence loop — measure accuracy under the *new* rules

### Why this matters
The biggest single weakness today. `recommendation_outcomes` has **1 row in
months**. `signal_outcomes.win_rate` (currently 36.2% rolling 30d) is
contaminated with pre-fix data. Until 30+ outcomes accumulate under the
post-2026-05-26 conviction overhaul + mutes + new bullish detectors, every
quality claim is hypothetical.

### Concrete actions
1. **Cohort dashboard** — new `/api/performance/cohort?since=YYYY-MM-DD`
   endpoint that filters `signal_outcomes` to a date floor and returns
   per-signal-type WR, avg P&L, and Wilson LB. UI panel under
   Performance → "Since rule change".
2. **Reco tracker telemetry** — daily cron logs row count + per-action
   breakdown (BUY/SELL/HOLD). Alert when it stays flat ≥ 3 days.
3. **A/B harness** — at scan time, randomly sample 20% of strong signals
   into a "control" cohort with LLM layers disabled and 80% into a
   "treatment" cohort (judge + debate + multi-perspective active). Both
   feed the tracker. After 60 days, compare WR + Sharpe between cohorts.
4. **Forced reco persistence for HOLD too** — relax
   `store_recommendation`'s "only BUY/SELL" filter; persist HOLDs with a
   `tracked=false` flag so the cohort has full visibility into what the
   engine considered but didn't act on.
5. **Sanity-check report** — Sunday-night cron emails (or Telegrams) a
   one-page weekly digest: signals fired by type/direction, WR by
   horizon, factor-contribution heatmap, top winners + losers.

### Effort
Moderate (3-5 days end-to-end).

### Done when
- A `/since=2026-05-26` query returns ≥ 100 evaluated outcomes
- Reco tracker shows ≥ 50 rows
- Weekly digest delivered by Monday 09:00 IST for 4 consecutive weeks
- A/B WR delta is computable with > 5% confidence

---

## 2. Live macro injection into LLM prompts

### Why this matters
Today's Indian-market briefing teaches the LLM the *rules* (FII direction
matters, USD-INR > 86 helps IT, Brent > $90 hurts OMCs). But the LLM
still doesn't know **today's** values, so it reasons in abstract. Closing
this is the difference between "knows the playbook" and "calls the play."

### Concrete actions
1. **`market_snapshot.py` service** — once per scan, fetch the day's
   macro tuple: previous-session FII net (₹Cr), DII net, India VIX, USD-INR
   close, Brent close, NIFTY 50 + Bank NIFTY % change, ADRs vs cash spread
   for top 10 ADR-listed names, top-3 sector index movers.
2. **Inject into prompt builders** — every LLM layer
   (judge, debate, multi-perspective, analyst) prepends a 6-line
   `LIVE MARKET (as of YYYY-MM-DD HH:MM IST)` block from this snapshot.
3. **Per-symbol macro hooks** — for each candidate, append sector index
   direction and the stock's ADR/GDR price differential.
4. **Caching** — snapshot refreshes every 15 min during market hours; LRU
   cache key is `(date, 15-min bucket)` to keep token cost flat.
5. **Sector rotation flag** — add `sector_rotation_state` to the snapshot
   (e.g. "IT outperforming Auto by 3.2% W/W"). Multi-perspective Macro
   agent gets the most lift from this.

### Effort
Moderate (2-3 days).

### Done when
- Every LLM call (post change) includes today's FII/DII/VIX/USD-INR/Brent
- Manual prompt audit shows the synthesiser citing concrete macro
  numbers, not generic "FII direction matters"
- Inference latency overhead < 50ms per call

---

## 3. LLM cost & latency discipline

### Why this matters
With all layers on (judge + debate + multi-perspective), a single scan
can fire **30-50 LLM calls** at peak. That's expensive (~$0.10-0.30/scan
on GPT-5-mini) and slow (2-4 min added to a 3-min scan). Best-in-class
systems run more thoughtfully *and* cheaper.

### Concrete actions
1. **Prompt-cache reuse** — Anthropic 5-min cache + OpenAI prompt caching
   for the Indian-market briefing (it's identical across every call in a
   scan). Should drop input-token cost ~70% on the second call onwards.
2. **Batched judging across all layers** — today the judge is batched
   (1 call ≤ 40 signals) but debate/multi-perspective are per-signal.
   Refactor multi-perspective to one batched call per perspective
   (4 calls total, not 4 × N).
3. **Quality-of-signal gating** — escalate layers based on strength bands:
   - strength 5-6: judge only
   - strength 7-8: judge + debate
   - strength 9-10: + multi-perspective
4. **Streaming UI** — show "AI reviewing..." spinners with partial fills as
   each LLM layer returns. Hides perceived latency.
5. **Token budget telemetry** — `/api/llm/usage/today` already exists; add
   per-layer cost breakdown + spend cap warnings in the popup status bar.
6. **Local-model fallback** — wire `llm_provider="ollama"` with a small
   local model (Qwen2.5-7B or Phi-3.5) for non-critical narrative work
   (signal enrichment). Keep frontier models only for the judge layer.

### Effort
Moderate (4-7 days).

### Done when
- Average scan cost ≤ $0.05 with all layers enabled
- Scan duration with all layers on ≤ current judge-only duration + 30%
- Spend cap warning fires before $0.50/day breach

---

## 4. Conversational reasoning + explainability UX

### Why this matters
agentX today is a one-shot popup. The competitive baseline (per the
6-repo audit, especially `indian-stock-ai-agent`) is a chat-style
research assistant: ask follow-up questions, see reasoning chains, drill
into specific factors. Without this, agentX feels like an alerter, not
an analyst.

### Concrete actions
1. **`/api/signals/{id}/chat` endpoint** — POST with a user follow-up,
   GET with `?stream=true` for SSE-streamed response. State persisted in
   `signal_chats` table keyed by signal_id + session.
2. **In-card chat thread** — expanded signal card shows a "Ask agentX"
   input. Threaded responses. Token cost surfaced inline.
3. **"Show your work" toggle** — every LLM verdict (judge / debate / MP)
   gets an expand-to-see-prompt-and-raw-response affordance. Helps debug
   when verdicts feel off.
4. **Per-factor drill-down** — clicking the multi-perspective specialist
   chips opens that specialist's full reasoning + the data it received.
5. **Saved questions library** — "compare with peers", "what would change
   this thesis", "risk of regime flip" as one-click prompt templates.

### Effort
Heavy (1-2 weeks).

### Done when
- Users can ask 3+ follow-ups per signal with full context retained
- Reasoning chain is inspectable for ≥ 80% of LLM verdicts
- Average user session length on the extension doubles

---

## 5. Regime adaptation — fix the 5y 32% WR problem

### Why this matters
This is the hardest open problem. 1y backtest = 49% directional WR,
5y = 32%. The strategy doesn't survive regime change. agentX's edges are
specific to the current 2024-2026 micro-regime; a 2018-2020 simulation
would be ugly. Best-in-class systems adapt to regime, not just react.

### Concrete actions
1. **Regime auto-detect** — extend `market_regime.py` to surface 4 states:
   trend_up, trend_down, range_bound, panic. Cross-check ADX + ATR
   percentile + VIX regime + Nifty 200-DMA distance.
2. **Per-regime strategy mix** — store per-regime model coefficients in
   `signal_edge`. In trend_up: weight breakout / 52w-high / quality
   breakout. In range_bound: weight rsi_extreme / NR7 / mean-reversion.
   In panic: defensive only (deep value at 52w-low with QV filter).
3. **Auto-mute on regime mismatch** — extend `DIRECTIONAL_MUTES` to a
   `REGIME_DIRECTIONAL_MUTES` keyed by (regime, signal_type, direction).
4. **Regime transition handler** — on detected regime change, the
   orchestrator should temporarily widen all conviction thresholds by
   25% for 5 sessions (let the new regime settle before placing
   high-conviction bets in it).
5. **Backtest harness extension** — rerun the walk-forward backtester
   stratified by regime so we can see which detectors carry edge in
   which regime. Output a "regime survival" heatmap.
6. **Cross-asset regime confirmation** — VIX + USD-INR + Brent + Gold
   form a 4-D regime vector. PCA against historical data → top-2 PCs
   define a 2-D regime plane that's more stable than single-axis ADX.

### Effort
Multi-week (2-4 weeks).

### Done when
- 5y backtest WR ≥ 45% (up from 32%)
- Detected regime change → strategy mix changes within 1 session
- Walk-forward shows positive Sharpe in ≥ 3 of 4 regime tiers

---

## 6. Real-time data + live broker execution

### Why this matters
We added AngelOne + Kite *adapters*, but the SDK install probably hasn't
happened in your env and credentials likely aren't configured. Until
that's done, agentX is still operating on 15-min-delayed yfinance ticks.
For intraday or even swing, that's a major edge loss.

### Concrete actions
1. **Setup wizard** — on first launch, the extension's Settings page
   detects no broker selected and offers a 3-step wizard: pick broker,
   paste creds, click "Test connection" → server attempts login and
   reports back. Today this is a manual paste + restart.
2. **Kite request_token flow in-app** — instead of asking the user to
   paste `access_token` daily, do the OAuth dance in the extension: a
   "Sign in with Zerodha" button opens the Zerodha login URL, captures
   the redirect's `request_token`, exchanges for `access_token`,
   persists encrypted.
3. **WebSocket tick feed** — both AngelOne and Kite expose live tick
   WebSockets. Add `app/services/broker_ws.py` that subscribes to
   watchlist + open-position symbols, pushes ticks into Redis pubsub +
   SSE to the popup. Replaces polling for the Live tab.
4. **Live order placement (with hard guardrails)** — wire
   `BrokerClient.place_order` with: dry-run mode (default), `--live` flag
   that requires user reconfirm + a second `LIVE_TRADING_ARMED=1` env
   var, hard daily exposure cap of ₹50,000 until proven.
5. **Trade journal** — every order (paper or live) writes to
   `trade_journal` with: broker, status, fill price vs intended price
   (slippage), timestamp, signal_id linkage. Audit trail.

### Effort
Heavy (2-3 weeks including the OAuth + WS work).

### Done when
- Settings → Broker shows "Connected ✓ (Kite, 15:42 IST)"
- Live tab shows sub-second tick updates
- A test ₹500 order can be placed and is reflected in the trade journal
- Daily exposure cap blocks orders past threshold

---

## 7. Options & derivatives as first-class — not just libraries

### Why this matters
We have **Black-Scholes Greeks**, **max-pain**, and **unusual options
activity** as pure-Python modules — but **none surface in the UI** and
**none feed the recommendation factor stack**. That's a wasted-built
feature. F&O is where institutional flow shows up in India; ignoring it
leaves a real edge on the table.

### Concrete actions
1. **Options factor** — add a new factor `options_positioning` to
   `recommendation.RecommendationFactors`. Score combines: max-pain
   distance, put/call OI ratio direction, unusual activity flags. Weight
   ~0.08 in the multi-factor stack.
2. **Options tab in the popup** — for any symbol, show: max-pain strike,
   ATM IV, IV percentile vs 30d, top-3 strikes by OI change, unusual
   activity flagged with direction hint.
3. **UOA as signal** — wire `detect_unusual_activity` to produce
   `unusual_options_activity` signals in the standard signal pipeline.
   Frontend label + timeframe already exist.
4. **IV-rank screener** — `/api/screener/iv-rank?gte=80` for finding
   names where IV is elevated relative to its 1-year range (sell-prem
   candidates). Bonus: pair-trade screener (long stock + short call).
5. **Greeks-aware position sizing** — when an open paper position is in
   the F&O segment, the risk_manager uses delta × position notional
   for risk budgeting, not just stop distance.
6. **Max-pain anchor in confluence** — within 5 days of monthly expiry,
   the confluence detector promotes signals whose direction aligns with
   the spot-vs-max-pain anchor. Penalises signals that fight it.

### Effort
Moderate (1 week).

### Done when
- New "Options" tab visible per stock with live max-pain + IV + UOA
- ≥ 5% of weekly signals are option-positioning-aware
- Recommendation engine factor breakdown shows `options_positioning`
  with non-zero contribution on F&O names

---

## 8. Portfolio-level enforcement (not just analytics)

### Why this matters
We added `portfolio_correlation` and the 10-rule risk gate as
**library functions**. They're not enforced inline in the auto-paper
trade path. So a user (or the auto-trader) can still pile into 5
correlated banks. Best-in-class systems prevent this *at decision time*,
not as a passive dashboard.

### Concrete actions
1. **Auto-paper-trader integration** — `auto_open_from_recommendations`
   must call `risk_gate.evaluate_trade` and reject when verdict is
   REJECTED, downgrade qty when MODIFIED. Today it doesn't.
2. **Correlation pre-trade check** — before opening any new position,
   compute `correlation_to_open` against current book; abort if ≥ 0.7
   *and* the user setting `allow_correlated_positions` is false.
3. **Portfolio risk dashboard** — popup tab showing: per-sector exposure
   (red bar > 25%), Beta to NIFTY, current portfolio heat (₹ at risk),
   correlation matrix heatmap of open positions, suggested rebalances.
4. **Rebalance recommendations** — weekly cron emits "trim ICICIBANK by
   30%, add ITC for sector balance" suggestions based on the heat dash.
5. **Hard sector cap on stop-loss orders too** — when the stop-loss
   monitor closes a position, the next entry in the same sector goes
   through the same gate (don't replace an SBI stop-out with another
   PNB long the same day).

### Effort
Moderate (3-5 days).

### Done when
- Auto-paper-trader REJECTS log lines visible in `agentx.log` when the
  10-rule gate blocks
- Portfolio dashboard renders sector + correlation heatmap
- A correlated-sector pile-up test (5 banks proposed) ends with 2 taken,
  3 rejected with a "sector cap reached" reason

---

## 9. Backtest + simulation rigor under realistic costs

### Why this matters
The walk-forward backtester is good, but it likely under-models real
execution friction on Indian trades: STT (0.1% each side), brokerage
(₹15-20 or 0.03%), DP charges (₹13-20 per scrip per day), exchange
charges (~0.0035%), GST on the above, **plus slippage** (typically 5-15
bps for mid-caps on entry-fill). A 52% WR system can be net-negative
after these.

### Concrete actions
1. **Realistic cost model** — extend `execution_costs.py` to a function
   `apply_costs(entry, exit, qty, segment, holding_days)` that returns
   net P&L. Plug into both the live tracker and the backtester.
2. **Slippage model** — for each historical bar in the backtester, the
   entry fill is `bar_open + 0.1× spread` for liquid names, scaled by
   `min(1.0, avg_daily_volume / 100_000)`. Slippage = 5-15 bps depending
   on volume tier.
3. **Monte Carlo on signal sequence** — instead of the standard
   chronological backtest, randomise the *order* of signals over 1,000
   iterations to derive a confidence interval on WR + Sharpe. Robustness
   check: if the 5th percentile is < 45% WR, the system is overfit.
4. **Per-segment backtester** — separate equity-cash, F&O, and intraday
   backtest tracks so we don't pretend a 5d-hold backtest applies to a
   day-trade.
5. **Drawdown stress test** — simulate the 2018-2020 bear / 2020 COVID
   crash with the current strategy mix. If max drawdown > 25%, mark the
   strategy as "regime-fragile" in the UI.
6. **Per-rule WR attribution** — the backtester already emits
   `signal_performance`. Add a `strategy_performance` table that
   attributes outcomes to (signal_type × LLM verdict × regime). Powers
   the "which layer adds value" answer.

### Effort
Heavy (1-2 weeks).

### Done when
- Backtest P&L is post-cost, post-slippage by default
- Monte Carlo 5th percentile WR ≥ 45% on 1y window
- 2018-2020 simulation report stored in `backtest_results/`
- Strategy_performance table populated with ≥ 10 (signal × verdict ×
  regime) cells

---

## Cross-cutting "10th": the boring fixes that compound

Not numbered to keep the list at 9, but listing because they matter:

- **`recommendation_outcomes` schema cleanup** — drop the BUY/SELL-only
  filter for tracking; persist all directional recommendations even
  when `action=HOLD` was the final call. Currently invisible.
- **Settings hot-reload** — toggling `debate_enabled` mid-scan should
  apply on the next iteration without backend restart. Today the loop
  caches `db_settings` per iteration but you should verify.
- **Secrets fixture in `test_secrets.py`** — the 4 failing tests are a
  CI smell. Fix the conftest schema bootstrap.
- **Frontend test crash** — the 2/170 frontend tests lost to a Vitest
  worker exit are environmental. Pin Vitest config, lock node version.
- **Docs** — there's no `docs/architecture.md` explaining the four LLM
  layers, the broker abstraction, and the risk gate. Anyone joining
  has to read the code. A 1-page diagram + 3-page narrative would 10×
  onboarding speed.

---

## Implementation order I'd recommend

If you can spend two months on this, here's the highest-ROI path:

| Week | Focus | Why |
|---|---|---|
| 1 | #1 Evidence loop | Without data you can't prove anything else worked |
| 2 | #2 Live macro injection | Cheap, high-leverage; makes every LLM call ground in today's reality |
| 3 | #6 Broker setup wizard + Kite OAuth | Switches the data source from 15-min-delayed to live; biggest *single* edge gain |
| 4 | #7 Options as first-class | Unlocks dormant features; visible product surface |
| 5-6 | #5 Regime adaptation phase 1 (detection + per-regime mix) | Hard but solves the 5y problem |
| 7 | #3 Cost & latency | Once the layers prove their worth, optimise their cost |
| 8 | #8 Portfolio enforcement | Auto-paper trader becomes safe to leave running |
| Later | #4 Conversational UI, #9 Backtest rigor | Lower urgency, both can wait until data shows what's working |

---

## What "best-in-class" actually means here

agentX today is **better-engineered than every open-source LLM stock
app I scanned (6 repos)**. The architecture is real, the methodology
is honest, the test coverage is unusual for a project at this stage.

But "best-in-class" requires three more things this 9-point list
addresses:

1. **Measurement** — you can show, on data, that the system outperforms
   a coin flip on directional calls, net of costs.
2. **Adaptation** — the system survives regime changes you didn't train
   on.
3. **Real money** — it can place actual trades, with audit, with hard
   risk gates, and with slippage modelled.

Today agentX has the *frame* for all three. The work above turns the
frame into the picture.

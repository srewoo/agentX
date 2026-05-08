# ADR-001 — Multi-factor weighted recommendation engine

Status: Accepted (in production as of swarm refactor)
Date: 2026-05-08
Deciders: Recommendation agent + architecture review

## Context

The Signals tab needs to rank Indian-market stocks for the user with a single
defensible "buy / avoid" score. The existing pipeline already produced atomic
signals (RSI, MACD, breakout, volume spike, pattern matches) but had no
unifying ranking layer.

Implementation lives in:
- `backend/app/services/recommendation.py` (123 stmts, 94% coverage)
- `backend/app/services/recommendation_factors.py` (105 stmts, 96% coverage)
- `backend/app/routers/recommendations.py`
- `backend/app/models/recommendation.py`

Constraints:
- Single-developer shop, no labelled training set, no MLOps pipeline.
- Latency budget for `/api/recommendations`: < 2s end-to-end on a single
  process.
- Output must be **explainable to a retail Indian investor** — "why is
  RELIANCE rated 78?". An opaque ML score fails this hard.
- Audit / regulatory posture: SEBI scrutiny on advisory tooling means we
  must be able to point at the exact factor and weight that drove a call.

## Decision

Adopt a **multi-factor weighted scoring engine** with explicit per-factor
weights and a transparent score breakdown. Not ML ranking.

Factor families currently scored (see `recommendation_factors.py`):
- Technical: RSI, MACD, breakout, trend-strength.
- Volume: relative-volume vs 20-day median.
- Pattern: chart-pattern hits.
- Relative strength vs sector index.
- Risk regime adjustment via `market_regime`.
- Fundamental tilt (P/E, debt/equity) where data available.

Weights live in code (not yet in DB) — easy to A/B but currently requires a
deploy. Each recommendation response includes the factor breakdown so the
extension can render "why this stock".

## Alternatives considered

### Option A — ML learn-to-rank (LightGBM / XGBoost)
- Pro: with enough labels, beats hand-tuned weights on out-of-sample data.
- Con: no labels, no feature store, no eval harness, no MLOps.
- Con: opaque to user; explainability needs SHAP.
- Risk: model rot once regime changes; nobody on the team to retrain.
- **Verdict: rejected — wrong phase of system maturity.**

### Option B — LLM-as-ranker
- Pro: zero feature engineering, natural language explanations.
- Con: non-deterministic, slow (1–3s per stock), expensive at scale, and
  prompt injection from scraped news is a real attack vector.
- Con: cost cap (`llm_client.py`) makes per-stock LLM ranking infeasible
  at >50 symbols.
- **Verdict: rejected as primary ranker. Used as a *narrative layer*
  on top of the score (see `llm_analyst.py`).**

### Option C — Multi-factor weighted scoring (chosen)
- Pro: deterministic, fast, explainable, cheap to run.
- Pro: each factor has unit tests; `recommendation_factors.py` is at 96%
  coverage. New factors plug in without touching ranking math.
- Con: weights are author-judgement, not learned. Risk of over-fitting
  to backtest cherry-picks.
- Con: factor weights live in code; tuning requires a deploy.

## Consequences

Positive
- Ships now, defensible, auditable, fast.
- Coverage on the engine is high (94/96%).
- Score breakdown surfaces in UI — users see *why*.

Negative / debt
- Weight tuning is engineering work, not a knob. **Move weights into
  `settings` table** (or a versioned `recommendation_profiles` table) so
  ops/QA can adjust without a release.
- No offline eval harness. We can't tell if a weight tweak made things
  better or worse without re-running the backtester (`backtester.py`
  is at 17% coverage — not trustworthy yet).
- Regime weighting silently mis-weights when `market_regime` returns
  something untested (12% coverage). This is a **load-bearing
  assumption** that needs hardening before we ship to >100 users.

## Reversibility

**Two-way door, with effort.**
- Replacing the engine with ML ranking later requires:
  1. Persisting all factor values per recommendation (we don't today).
  2. Capturing realised outcomes (`signal_outcomes` partially does this).
  3. Building eval harness.
- The router contract `{ symbol, score, factors[], rationale }` was
  designed to accommodate either backend. Swap is internal.

## Rollback path

If the engine produces obviously broken rankings in prod:
1. Feature flag `recommendation.enabled = false` in `settings` (not yet
   wired — TODO).
2. Fall back to ranking by raw `signal.score` from `signal_engine`.
3. UI gracefully degrades: "Recommendations temporarily unavailable, showing
   raw signals."

Rollback target: < 5 minutes to disable, no data migration required.

## Open questions

- Should weights be per-user (risk_mode already exists: conservative /
  balanced / aggressive)? Today only thresholds vary. Weights should too.
- How do we evaluate without a labelled set? Realistic options: paper-trade
  the recommendation list for 30 days against a benchmark (Nifty 50) and
  compare drawdown + hit-rate.

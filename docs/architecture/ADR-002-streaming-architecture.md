# ADR-002 — Real-time quote streaming architecture

Status: Accepted with caveats — **WS auth gap is BLOCKING for prod**
Date: 2026-05-08

## Context

The Live tab requires sub-second price ticks for the user's watchlist
(typically 5–30 symbols). Implementation lives in:

- `backend/app/services/streaming/quote_stream.py` — `QuoteHub`, single
  upstream subscription per symbol, fan-out to N WS subscribers.
- `backend/app/services/streaming/poll_fallback.py` — yfinance polling
  source.
- `backend/app/services/streaming/broker_kite.py` — Zerodha Kite stub
  (not connected).
- `backend/app/routers/stream.py` — `WS /api/stream/quotes`.

Per-subscriber bounded queue: 256 messages, oldest-dropped on overflow.
Heartbeat: 15s server→client ping. Per-conn cap: 100 symbols.

Today the QuoteHub is **single-process**. Redis pub/sub channels with
prefix `stream:ticks:<SYMBOL>` are *prepared* in the code (see comment
`quote_stream.py:9-11`) but the broadcast wire-up across processes is
not validated end-to-end.

## Decision

Adopt a **three-phase streaming roadmap**, currently at Phase 1.

- **Phase 1 (now):** Single-process `QuoteHub` with poll-based upstream.
  yfinance fallback. One uvicorn worker. OK for ≤ 100 concurrent users.
- **Phase 2 (100 → 10K users):** Redis pub/sub fan-out across 2–5
  uvicorn workers behind a sticky-session load balancer. Existing
  `stream:ticks:<SYMBOL>` channel design is the migration path. Upstream
  pollers still in-process but de-duplicated via a Redis lock so only one
  worker polls a given symbol.
- **Phase 3 (10K+ users):** Replace yfinance polling with broker WS
  (Kite Connect / Angel One SmartAPI). Upstream pollers move to a
  dedicated `quote-ingestor` service. WS terminator can scale
  horizontally; ingestion stays vertically scoped per broker connection
  limit.

## ⚠️ BLOCKING SECURITY GAP — WS bypasses API-key auth

Highlighted by agent ac1237140dffe26b5's audit. Confirmed in this review.

`backend/app/main.py:240-314` — the `request_logging_middleware` only
wraps `_handle_request`, which performs API-key validation **for HTTP**.
The WS upgrade path enters via Starlette's WebSocket route directly and
**never hits `_handle_request`**. As a result:

> Anyone who can reach the backend over the network can open
> `WS /api/stream/quotes?symbols=...` without `X-API-Key` and consume
> ticks indefinitely.

Severity: **High** for any deployment beyond `localhost`. Mitigation
options, in order of preference:

1. **Validate API key inside the WS handler before `accept()`** —
   read `X-API-Key` from the upgrade headers (browsers don't allow
   custom headers on `new WebSocket()`, so combine with a
   `?token=` query param signed by the extension's stored key).
2. **Per-connection short-lived ticket**: `POST /api/stream/ticket`
   (HTTP, auth'd) returns a 60s JWT; WS endpoint validates it.
3. Front the backend with nginx that enforces `X-API-Key` on the
   `/api/stream/` location too.

This must be fixed before any non-localhost deployment. Recommend
engaging the `security-reviewer` subagent to vet the chosen approach.

## Alternatives considered

### Option A — Single-process QuoteHub (chosen, Phase 1)
- Pro: zero infra, fast to ship, one source of truth.
- Pro: bounded queue + drop-oldest prevents slow-consumer back-pressure.
- Con: single point of failure, can't scale horizontally.
- Con: in-process polling means N workers = N upstream calls per symbol
  unless de-duped (today: not de-duped).
- Risk: yfinance throttles or returns stale data; no SLA.

### Option B — Redis pub/sub fan-out (Phase 2 target)
- Pro: lets us run 2–5 workers behind a load balancer.
- Pro: Redis already a dependency for caching.
- Con: every tick crosses the Redis wire — adds 1–5ms latency.
- Con: need a coordinator to ensure exactly-one upstream poller per
  symbol (Redis SETNX lock with TTL is the simplest path).
- Risk: Redis becomes a SPOF — needs Sentinel/Cluster for HA.

### Option C — Broker WebSocket (Kite, Angel) — Phase 3
- Pro: real exchange ticks, sub-100ms, no scraping ToS issue.
- Con: one Kite connection limit per API key; need to multiplex.
- Con: per-broker integration burden; must handle reconnection,
  out-of-order ticks, market-hours gating.
- Pricing: Kite Connect ₹2K/mo per developer key (as of 2026), plus
  user-level OAuth for retail accounts.
- Risk: Vendor lock-in unless we abstract via the existing
  `QuoteSource` Protocol — which we did (`quote_stream.py:47-58`).

## Scaling triggers — when to advance phase

| From → To | Trigger metric | Threshold |
|---|---|---|
| 1 → 2 | Concurrent WS connections | > 50 sustained |
| 1 → 2 | Worker CPU at peak | > 70% on single worker |
| 1 → 2 | yfinance 429 rate | > 1% of polls |
| 2 → 3 | DAU | > 5K |
| 2 → 3 | Tick staleness P95 | > 3s |
| Any | yfinance ToS strike / IP block | Immediate |

## Consequences

Positive
- Phase 1 ships today. Slow-consumer protection is in place.
- `QuoteSource` Protocol means broker swap is an internal change.

Negative / debt
- WS auth gap (above) — must fix before any public deploy.
- No metrics on `sub.dropped` until disconnect (`stream.py:94-96`) —
  add a Prometheus counter (see ADR-005).
- Redis pub/sub code path (the `stream:ticks:*` channel) exists but is
  not exercised by tests. Coverage on `quote_stream.py` is 79% but the
  Redis-connected branch is in the missing 21%.

## Reversibility

- Phase 1 → 2: **two-way door** — Redis fan-out is additive; turn off by
  unsetting `redis_url` and traffic goes back to in-process.
- Phase 2 → 3: **one-way door per broker integration**. Once users
  OAuth into a broker, ripping that out is user-visible. Pick the broker
  abstraction layer carefully and resist leaking Kite-specific shapes
  beyond `streaming/broker_kite.py`.

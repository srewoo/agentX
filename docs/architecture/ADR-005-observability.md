# ADR-005 — Observability: structured logs, tracing, metrics

Status: Proposed
Date: 2026-05-08

## Context

Today we are mostly blind in production. What exists:

- Stdlib logging configured in `backend/app/main.py:59-91` with a
  rotating file handler (5 MB × 5 = 25 MB cap).
- Plain text format: `"%(asctime)s %(levelname)-5s [%(name)s] %(message)s"`.
- Per-request correlation ID generated in
  `backend/app/main.py:243`: `req_id = uuid.uuid4().hex[:8]`. Stamped
  on `X-Request-Id` response header (`main.py:274`).
- Quiet path filter for `/api/health`, `/docs`, `/openapi.json`,
  `/redoc`, `/` (`main.py:250`).
- Third-party loggers explicitly silenced (`main.py:81-89`).

What is missing — concrete, file-cited:

1. **No structured JSON logs.** `main.py:56` defines a text format.
   You cannot ingest this into Loki / Datadog / Elastic without lossy
   regex parsing.
2. **`req_id` does not propagate into service-layer logs.** It is a
   local variable in the middleware closure. Every `logger.info(...)`
   in `app/services/*.py` is correlation-blind. Example:
   `app/services/orchestrator.py` and `app/services/llm_client.py`
   log without any request context — when an LLM call inside an
   HTTP request fails, you cannot tie the log line back to the user
   request.
3. **No tracing.** No OpenTelemetry, no spans. The most expensive
   path in the system —
   `recommendations router → recommendation.py → factors → llm_analyst
   → llm_client → external API` — has zero per-hop timing data. We
   know request total duration (`main.py:262 duration_ms`), nothing
   else.
4. **No metrics endpoint.** No `/metrics` Prometheus surface.
   - LLM cost cap is enforced (`llm_client.py`) but its current spend
     is not exposed as a gauge.
   - Rate limiter buckets are not metered.
   - `QuoteHub` subscriber count and `sub.dropped` are not exposed
     (`stream.py:94-96` only logs at disconnect).
   - Orchestrator scan duration / failures: not metered.
5. **No SLOs.** No defined latency or error budget for any endpoint.
6. **No alerting.** Rate-limited requests log `WARNING`
   (`main.py:289`) but no monitor is wired.
7. **WS observability gap.** Per-connection drop counter only printed
   at disconnect. A subscriber stuck dropping 1000 messages gets
   logged exactly once at the end.

## Decision

Adopt a three-layer Python observability stack:

### Layer 1 — Structured logging (Loguru or structlog)

Replace stdlib config in `main.py:59-91`. Recommend **structlog**:

- JSON output by default in prod (env-flagged for local dev).
- `contextvars`-based context: bind `req_id`, `user_id` (when ADR-003
  lands), `route`, `client_ip` once in middleware → every downstream
  `logger.info` carries it automatically.
- Strip secrets at the formatter layer (regex `(?i)(api_key|token|
  password)`).

### Layer 2 — Tracing (OpenTelemetry)

- `opentelemetry-instrumentation-fastapi` for inbound spans.
- `opentelemetry-instrumentation-httpx` and `aiosqlite` for downstream
  spans.
- Manual spans around: `signal_engine.run`, `recommendation.compute`,
  `llm_client.call`, `notifications.dispatch`, `QuoteHub.publish`.
- Export to OTLP — local: stdout exporter; staging: Tempo / Jaeger;
  prod: Datadog or Honeycomb.
- Sampling: 100% in dev, head-based 10% in prod, **tail-sample 100%
  of error spans**.
- Propagate W3C `traceparent` from the extension → backend so the
  Chrome-side timing is part of the same trace once we add
  `@opentelemetry/instrumentation-fetch` to the popup.

### Layer 3 — Metrics (Prometheus)

- `prometheus_fastapi_instrumentator` for HTTP basics: requests by
  route, status, latency histogram.
- Custom gauges/counters:
  - `llm_cost_usd_today{provider}` (gauge)
  - `llm_calls_total{provider,model,outcome}` (counter)
  - `rate_limit_drops_total{bucket}` (counter)
  - `ws_connections_active{}` (gauge)
  - `ws_messages_dropped_total{}` (counter, fixes the
    `stream.py:94-96` end-of-life-only logging)
  - `orchestrator_scan_duration_seconds{phase}` (histogram)
  - `notification_send_total{channel,outcome}` (counter)
- Expose at `/metrics`. Keep auth — even metrics can leak load patterns.

## SLOs to define alongside this work

Starter set, refine after one month of data:

| Endpoint | SLI | Target |
|---|---|---|
| `/api/recommendations` | P95 latency | < 1.5s |
| `/api/stocks/{sym}/ai-analysis` | P95 latency | < 4s |
| `WS /api/stream/quotes` | tick fan-out → ws send | < 200ms |
| Any HTTP route | 5xx rate | < 0.1% |
| Notifications | end-to-end deliver time | < 30s P95 |

Burn-rate alerts: 14× / 1h and 6× / 6h windows.

## Alternatives considered

- **Loguru** vs structlog: Loguru has nicer ergonomics, but structlog's
  contextvars + processor pipeline is a better fit for FastAPI's
  per-request scoping and for stripping secrets centrally.
- **Datadog APM agent only**: simpler to install, vendor lock-in,
  expensive at scale. Acceptable for Phase 2; OTel keeps us portable.
- **Sentry only**: great for errors, no metrics or tracing. Use it
  *alongside* (already standard) for error grouping, not instead of.

## Consequences

Positive
- Every prod debug session improves by an order of magnitude.
- LLM cost gauge means we see the cap approaching, not just hitting.
- Tracing makes it possible to optimise the slow paths instead of
  guessing.

Negative / cost
- Adds dependencies: `structlog`, `opentelemetry-*`,
  `prometheus_fastapi_instrumentator`.
- Tracing adds ~1% CPU overhead and serialisation cost — acceptable.
- JSON logs are bigger on disk than text. Rotate more aggressively or
  ship to a backend.

## Reversibility

**Two-way door.** structlog is a drop-in replacement; OTel can be
disabled by env; metrics endpoint is additive. None of this changes
data shape or contracts.

## Implementation order (suggested PRs)

1. structlog + JSON logs + `req_id` contextvar propagation. Smallest,
   biggest payoff.
2. `/metrics` endpoint with the LLM cost gauge first (immediate value).
3. OTel FastAPI + httpx + aiosqlite instrumentation.
4. Manual spans around the slow paths.
5. SLO definitions + monitor wiring.

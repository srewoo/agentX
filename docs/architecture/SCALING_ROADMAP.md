# Scaling Roadmap

Phased plan for agentX from today's single-user local install to a
hosted product. Cost estimates are USD/month, rough order-of-magnitude
(±50%), assume `ap-south-1` (Mumbai) for data residency.

## Phase 0 — Today (1 user, 1 process)

**Where we are.** Single uvicorn worker, SQLite, optional Redis,
yfinance polling, single Chrome extension install.

Architecture:
- 1× FastAPI process on user's laptop or a `t3.small`-class VM.
- SQLite WAL with one writer.
- Redis optional (in-memory cache fallback).
- yfinance + NSE scrape for prices, LLM provider per the user's key.

Limits before hitting walls:
- WS connections: ~50 before single-process CPU saturates on JSON
  serialisation.
- HTTP RPS: ~200 sustained on a single worker before tail latency
  blows up.
- SQLite: fine until concurrent writers; today there is one.

Cost: $0–$30/mo if self-hosted on a small VM.

Required to leave Phase 0:
- WS auth gap fixed (ADR-002).
- structlog + correlation propagation (ADR-005 layer 1).
- venv pinned to Python 3.11 (per `INTEGRATION_TODO.md`).

---

## Phase 1 — Up to 100 users (private beta)

Audience: invited testers, mostly Indian retail traders. Still
single-region, single-AZ.

Required architectural changes:
- **Auth**: introduce real users (not just `X-API-Key`). OAuth via
  Google for retail simplicity. Adds `users`, `user_secrets` tables.
  Implements ADR-003 multi-tenancy schema (`user_id` on every owned
  table). Backfill existing rows with a sentinel UUID.
- **Secrets**: env-only for shared infra creds; KMS-wrapped column
  for per-user secrets (ADR-003).
- **Postgres migration** from SQLite. Single instance,
  `db.t4g.small`. Migration script: dump → import; schemas are simple
  enough that a one-shot rewrite is cheaper than a dual-write window
  at this scale. Keep SQLite as the local-dev path.
- **Rate limiter on Redis** (ADR-004). Required before workers > 1.
- **Observability layer 1 + 2** (ADR-005): JSON logs, `/metrics`,
  basic OTel.
- **Notification rate limits per user** to bound Twilio/SendGrid
  spend.

Infra:
- 2× FastAPI workers on a single VM, or 2× containers on ECS/Fargate
  / `t3.medium`.
- Redis: managed, single node (ElastiCache `cache.t4g.micro`).
- Postgres: RDS `db.t4g.small`, single AZ.
- ALB or Cloudflare in front (TLS termination, edge rate limit).
- Sentry for errors, Grafana Cloud free tier for metrics.

Trigger metrics to advance to Phase 2:
- DAU > 80 sustained for 7 days.
- WS concurrent peak > 50.
- yfinance 429 rate > 1% of polls.
- p95 latency on `/api/recommendations` > 1.5s.

Estimated cost: ~$80–$150/mo.

---

## Phase 2 — Up to 10K users

Audience: open beta or paid product. Multi-AZ, still single-region.

Required architectural changes:
- **Streaming on Redis pub/sub fanout** (ADR-002 Phase 2). 2–5
  uvicorn workers, sticky session on the WS path. One upstream poller
  per symbol via Redis SETNX lock.
- **Broker integration begins** — sign Kite Connect partner agreement
  (~₹2K/mo per developer key + per-user OAuth). yfinance becomes
  fallback only.
- **Outbox pattern** for notifications: write `outbox` row in same
  Postgres tx as the alert, async worker dispatches. Drops the
  fire-and-forget risk currently in `notifications.py`.
- **Per-user LLM cost cap** in addition to global cap. Reject
  requests when user is over budget; UI shows quota.
- **Background workers extracted**: orchestrator scan loop moves out
  of the API process into a dedicated worker container. Same code,
  different entrypoint.
- **Observability layer 3** (ADR-005): tail-sampled traces, SLO burn
  alerts wired to PagerDuty.
- **Schema migrations** under Alembic; forward-only; review every
  migration for online-safety (no full table locks during peak).

Infra:
- 3–5× API containers (autoscale on CPU + WS conn count).
- 2× scan-worker containers.
- Redis: ElastiCache cluster mode, primary + 1 replica
  (`cache.r7g.large`).
- Postgres: RDS `db.r7g.large`, multi-AZ, read replica for analytics.
- Cloudflare in front, WAF on, edge rate limits.
- Datadog or Grafana Cloud paid; PagerDuty.
- ECS Fargate or small EKS — pick one and stick.

Trigger metrics to advance to Phase 3:
- DAU > 8K.
- WS concurrent peak > 4K.
- Postgres write IOPS > 60% of provisioned.
- Redis CPU > 70% peak.
- > 3 distinct bounded contexts have independent deploy needs (i.e.
  the streaming team and the recommendation team start blocking each
  other).

Estimated cost: ~$1.2K–$2.5K/mo (excl. broker fees + LLM spend).

---

## Phase 3 — Up to 100K users

Audience: scaled product. Likely paid tiers. Multi-region only if
we're serving non-IN users; for IN-only, single region with
multi-AZ + active-passive DR is enough.

Required architectural changes:
- **Service decomposition** — extract along the bounded-context lines
  in `SYSTEM_OVERVIEW.md §7`:
  - `quote-ingestor` (broker WS terminator, owns Redis publish).
  - `recommendation-service` (computes scores, no I/O outside its
    DB + cache).
  - `notification-service` (Kafka consumer, owns channel adapters).
  - `api-gateway` (FastAPI, thin — auth, rate limit, request routing).
  - Keep `signals` and `portfolio` together until they prove they
    need to split.
- **Kafka** for inter-service async (per CLAUDE.md). MSK Serverless
  or self-managed on K8s. Topics: `signals.*`, `tracking.*`,
  `cmd.notifications.*`, `dlq.*`. Outbox → Debezium → Kafka.
- **Postgres sharding** if a single RDS write node is the bottleneck.
  Most likely we'll add read replicas + caching first; sharding only
  if write IOPS justify it.
- **CDN for static assets** (extension assets if we ship a hosted UI).
- **Feature flags** (LaunchDarkly or self-hosted Unleash).
- **K8s** at this scale — autoscaling policies per service, PDBs,
  HPAs on RPS + CPU.
- **Cost controls**: per-tenant LLM budgets enforced upstream; cold
  paths moved to cheaper models; aggressive caching on
  recommendation outputs (TTL 5min for non-watchlist symbols).

Infra:
- EKS, ~20–50 pods steady state.
- RDS Aurora Postgres, multi-AZ, 2 readers.
- ElastiCache Redis cluster, 3 shards × 2 nodes.
- MSK Serverless or 3-broker MSK provisioned.
- Cloudflare Enterprise or AWS WAF.
- Datadog full stack.
- Per-customer KMS keys for enterprise tier (if any).

Trigger metrics to advance further:
- > 100K DAU and growth not slowing.
- Multi-region demand.
- B2B / enterprise tier with custom SLAs.

Estimated cost: ~$15K–$40K/mo (excl. broker, LLM spend, Datadog
which can dominate).

---

## Phase 4 — Platform (>100K users, multi-team)

At this scale architecture concerns shift from scaling to
**enabling teams**:

- Internal platform (golden paths, self-service deploy).
- Service mesh (Istio or Linkerd) for mTLS + retries + circuit
  breakers without per-service code.
- Per-team ownership boundaries: each bounded context has an oncall
  rotation, a runbook, an SLO, and a budget.
- Schema registry for Kafka events (Avro/JSON Schema).
- Vault for dynamic secrets (replaces ad-hoc KMS columns).

Out of scope for this document — re-decide when we get close.

---

## Cross-phase invariants

These do not change with scale:

- One service per bounded context, no shared DB.
- Every state change emits a usage-tracking event (CLAUDE.md §1).
- Every PR has tests (CLAUDE.md §9).
- Defensive router imports in `main.py` get removed before Phase 1.
  They were a swarm-landing convenience; they hide real bugs.
- Migrations are forward-only.
- No Friday deploys outside P0/P1.

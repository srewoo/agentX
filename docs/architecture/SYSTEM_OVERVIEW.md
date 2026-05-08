# System Overview — agentX

Author: Priya (Staff Architect review)
Status: Snapshot of post-13-agent-swarm refactor. Read alongside `INTEGRATION_TODO.md` and `COVERAGE_REPORT.md`.

## 1. High-level topology

```
                 ┌────────────────────────────────────────────┐
                 │  Chrome Extension (MV3, React 18, Vite)    │
                 │  extension/src/popup/*                     │
                 │   - Tabs: Live / Signals / Watchlist /     │
                 │     Portfolio / Alerts / Settings          │
                 │   - WS singleton hook (useWS)              │
                 │   - lightweight-charts                     │
                 │   - Content script: 12 IN-finance domains  │
                 └──────────────┬─────────────────────────────┘
                                │ HTTPS  +  WS
                                │ X-API-Key (HTTP only — see ADR-002)
                                ▼
        ┌───────────────────────────────────────────────────────┐
        │ FastAPI single-process app  (backend/app/main.py)     │
        │                                                       │
        │  Middleware: req-id → log → CORS → rate-limit (dict)  │
        │              → API-key auth → 60s timeout             │
        │                                                       │
        │  Routers:                                             │
        │   /api/signals  /api/watchlist  /api/settings         │
        │   /api/stocks   /api/analysis   /api/market           │
        │   /api/performance /api/alerts  /api/screener         │
        │   /api/backtest /api/portfolio  /api/llm/*            │
        │   /api/recommendations                                │
        │   WS /api/stream/quotes  ◄── auth bypass (see ADR-002)│
        │                                                       │
        │  In-process services:                                 │
        │   orchestrator (asyncio scheduler)                    │
        │   QuoteHub  (single-process fan-out)                  │
        │   cache_manager (Redis OR in-mem fallback)            │
        │   signal_engine, recommendation, portfolio, etc.      │
        └─────┬─────────────────┬────────────────┬──────────────┘
              │                 │                │
              ▼                 ▼                ▼
        ┌──────────┐      ┌──────────┐    ┌────────────────────┐
        │ SQLite   │      │ Redis    │    │ External (egress)  │
        │ (WAL)    │      │ (opt.)   │    │  yfinance / NSE    │
        │ agentdb  │      │ cache +  │    │  Gemini/OpenAI/    │
        │ .rvf     │      │ pub/sub  │    │   Anthropic LLM    │
        │          │      │  prefix  │    │  Telegram, Twilio, │
        │          │      │ stream:  │    │   SendGrid, FCM    │
        │          │      │ ticks:*  │    │  Kite (stub)       │
        └──────────┘      └──────────┘    └────────────────────┘
```

## 2. Persistence map

All tables live in a single SQLite file (`DB_PATH = settings.sqlite_path`, see `backend/app/database.py:12`).

| Table | Owner service | Notes |
|---|---|---|
| `signals` | `signal_engine` | Generated signals; consumed by recommendation & UI. |
| `watchlist` | router/watchlist | No `user_id` column. Single-tenant. |
| `settings` | router/settings | **Plaintext** API keys, Telegram tokens, Twilio creds. See ADR-003. |
| `signal_outcomes` | `signal_tracker` | Outcome tracking for dynamic weighting. |
| `price_alerts` | `alert_checker` | Triggered by orchestrator scan loop. |
| `signal_performance` | `signal_tracker` | Seeds `seed_performance_cache()` at startup. |
| `backtest_runs` | `backtester` | Persisted backtest results. |
| `llm_usage` | `llm_client` | New in this refactor — feeds daily USD cap + `/api/llm/usage`. |

Redis is **optional** today. `cache_manager` falls back to in-memory if `redis_url` is unset (`main.py:100`). When connected, also acts as pub/sub for tick fan-out (`stream:ticks:<SYMBOL>`).

## 3. Data flow — extension ↔ backend

### 3.1 Sync request (Live tab tile, recommendation, screener)
```
Popup → fetch(/api/...) with X-API-Key header
   → middleware (rate-limit, auth, 60s timeout)
   → router → service → SQLite/Redis/external API
   → JSON envelope { data, ... }
```

### 3.2 Streaming quotes
```
Popup useWS hook → WS /api/stream/quotes?symbols=...
   → stream router (NO API-key check; see ADR-002)
   → QuoteHub.add_subscriber → poll_fallback or broker_kite
   → Tick → per-conn bounded queue (256) → ws.send_text
```

### 3.3 Async / background
```
Orchestrator (asyncio task started in lifespan)
   ├─ scan loop → signal_engine → signals table
   ├─ alert_checker → notifications.dispatch
   │     └─ channels/{telegram,email,whatsapp,sms,push}
   └─ signal_tracker → signal_performance
```

## 4. Async / sync boundaries

- **Sync**: HTTP routers, SQLite reads/writes (aiosqlite is async at the driver but SQLite serialises writes via WAL).
- **Async fire-and-forget**: notification channels, llm_usage row insert, WS broadcasts.
- **Background long-running**: orchestrator scan loop, QuoteHub upstream poller, NSE fetcher.

There is **no real queue** today. Every async task lives inside the same process. If the FastAPI process dies, in-flight notifications and ticks are lost.

## 5. Observability — what's there, what's missing

Present:
- Stdlib logging with rotating file handler (`main.py:71-78`), 5 MB × 5 = 25 MB cap.
- Per-request correlation ID via `req_id = uuid.uuid4().hex[:8]` (`main.py:243`), stamped on `X-Request-Id` response header.
- Quiet path filtering (`/api/health`, `/docs`, `/openapi.json`).

Missing (specifics — see ADR-005):
- **No structured logging.** Plain `%(asctime)s %(levelname)-5s [%(name)s]` text — not JSON. Cannot ingest into Loki/Datadog without regex.
- **No `correlationId` propagation** into service-layer logs. `req_id` is local to the middleware closure.
- **No tracing.** No OpenTelemetry, no spans across `signal_engine → llm_client → external API`.
- **No metrics endpoint.** No `/metrics` Prometheus surface. LLM cost cap is enforced but not exposed as a gauge.
- **No DLQ / poison-pill handling** for failed notification sends — only logger.warning.
- **WS observability gap**: `sub.dropped` is logged only at disconnect (`stream.py:94-96`). No live counter, no alert on slow consumers.
- **No SLOs defined.** P95/P99 latency for `/api/recommendations`, `/api/stocks/*/ai-analysis`, WS message delivery — none captured.

## 6. Known cross-cutting concerns

1. **WS bypasses API-key auth.** `_handle_request` is HTTP-only (`main.py:278`); WS upgrades skip it. See ADR-002.
2. **Rate limiter is per-process.** `_rate_buckets` is a module-level `defaultdict`. With `--workers 2+` the effective limit doubles. See ADR-004.
3. **Single-tenant.** No `user_id` / `tenant_id` on any table. See ADR-003.
4. **Secrets at rest are plaintext.** `settings` table stores LLM keys, Telegram bot tokens, Twilio SIDs. Encrypted only by the host filesystem. See ADR-003.
5. **Defensive router imports** mask boot-time errors (`main.py:33-53`). Useful during the swarm landing, but a permanent risk: a typo silently disables `/api/portfolio/*`. Convert to hard-fail before GA.

## 7. Service responsibility boundaries (current)

| Bounded context | Service | Owns |
|---|---|---|
| Market data ingestion | `data_fetcher`, `nse_fetcher`, `market_data` | Symbol quotes, OHLC, fundamentals fetch. |
| Signal generation | `signal_engine`, `patterns`, `technicals` | Indicators, pattern detection, signal scoring. |
| Recommendation | `recommendation`, `recommendation_factors` | Multi-factor weighted ranking. |
| Portfolio | `portfolio` | FIFO P&L, Sharpe, drawdown, beta. |
| Alerts | `alert_checker`, `notifications`, `channels/*` | Threshold checks + multi-channel routing. |
| Streaming | `streaming/quote_stream`, `streaming/poll_fallback`, `streaming/broker_kite` | Live tick fan-out. |
| LLM | `llm_client`, `llm_analyst` | Provider abstraction, retry, cost cap, usage logging. |
| Risk / regime | `risk_manager`, `market_regime`, `market_rules` | Position sizing, regime classification. |

These look like reasonable bounded contexts. They live in the same process today; that is fine for a single user. The roadmap (`SCALING_ROADMAP.md`) covers when to extract.

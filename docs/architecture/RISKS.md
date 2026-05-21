# Top 10 Risks

Severity = blast radius if it triggers. Likelihood = chance in next 90
days at current trajectory. Both: H / M / L.

---

## 1. WebSocket bypasses API-key auth

- **Severity: H** — anyone reachable on the network can stream ticks
  without auth.
- **Likelihood: H** — exists in `main.py` middleware design, confirmed
  in this review (see ADR-002).
- **Where**: `backend/app/main.py:240-314` (middleware is HTTP-only);
  `backend/app/routers/stream.py:59-66` (no auth before `accept()`).
- **Mitigation**: Validate API key inside the WS handler before
  `accept()`. Combine with short-lived signed ticket from a
  HTTP-authed `POST /api/stream/ticket`. Block any non-localhost
  deploy until fixed. Engage `security-reviewer` subagent.

## 2. SQLite WAL contention on concurrent writes

- **Severity: M** — write stalls cascade into 504s on
  `/api/portfolio/transactions`, `/api/alerts`, etc.
- **Likelihood: M** — already observed by the
  `test_portfolio.py::concurrent writes` integration test
  (see `INTEGRATION_TODO.md`). Today single-user; will detonate the
  moment we add a second user with a busy alert checker.
- **Where**: `backend/app/database.py` (single SQLite file, WAL mode).
- **Mitigation**: Migrate to Postgres at Phase 1 of the roadmap.
  Until then, document max-concurrency on the deployment and don't
  parallelise the orchestrator scan loop.

## 3. Plaintext API keys / Telegram tokens / Twilio creds at rest

- **Severity: H** — any backup leak or second user = full credential
  compromise. Twilio especially: an attacker with the SID/token can
  burn through balance fast.
- **Likelihood: M** — fine for single-user local install; high-risk
  the moment we host or back up.
- **Where**: `backend/app/database.py:42-47` (settings table KV);
  `backend/app/routers/settings.py` (read side now redacts but
  storage is plaintext).
- **Mitigation**: ADR-003 — env-only for shared infra creds,
  KMS-wrapped column for per-user secrets. Document the risk in
  README until then.

## 4. yfinance ToS / scraping bans

- **Severity: H** — primary price source disappears. Live tab,
  signals, recommendations all stop working.
- **Likelihood: M** — Yahoo has tightened scraping enforcement before;
  yfinance is unofficial. Risk grows with our QPS.
- **Where**: `backend/app/services/data_fetcher.py`,
  `backend/app/services/streaming/poll_fallback.py`.
- **Mitigation**: Treat yfinance as a *fallback*, not a primary. Sign
  Kite Connect or Angel One SmartAPI by Phase 2 (scaling roadmap).
  Cache aggressively (already do via `cache_manager`). Per-IP
  request budget with backoff; honour 429 with exponential delay.

## 5. Twilio / SendGrid / FCM cost explosion

- **Severity: H** — a runaway alert loop or compromised account can
  send tens of thousands of SMS/WhatsApp in minutes. SMS to India is
  ~₹0.20/msg = ₹2K per 10K messages.
- **Likelihood: M** — `notifications.py` is fire-and-forget; no
  per-user cap; alert dedup logic gates lives in `alert_checker.py`
  whose tests are *currently failing* (`INTEGRATION_TODO.md` §1).
- **Where**: `backend/app/services/notifications.py`,
  `backend/app/services/channels/{sms,whatsapp,email}.py`,
  `backend/app/services/alert_checker.py`.
- **Mitigation**:
  - Per-user, per-channel daily cap (e.g. 50 SMS/day).
  - Per-channel global cap (kill switch).
  - Outbox + worker pattern so a runaway loop is bounded by the
    worker queue size, not the alert loop's CPU.
  - **Fix the failing alert dedup tests before any hosted deploy.**

## 6. LLM cost-cap evasion

- **Severity: M** — daily USD cap exists but several paths bypass it.
- **Likelihood: M** — multi-provider fallback chain is at 63%
  coverage; the cap-enforcement edge cases live in the missing 37%.
- **Where**: `backend/app/services/llm_client.py` (cost cap +
  `llm_usage` table), `backend/app/services/llm_analyst.py`.
- **Specific evasion paths to audit**:
  - Streaming responses — does `record_llm_usage` see partial
    completions if the connection drops mid-stream?
  - Provider fallback after a rate limit — does the second provider
    re-check the cap?
  - Cached LLM responses — are cache hits counted (they shouldn't be,
    but consistent accounting matters)?
  - The 2 currently-failing `test_llm_client.py` tests
    (`INTEGRATION_TODO.md` §3) are exactly in the cap path.
- **Mitigation**: cover the missing branches; add an explicit
  pre-call check that fails closed if the cap query errors;
  per-user cap once auth lands.

## 7. NSE 403 cascade

- **Severity: H** — NSE is a primary fundamentals/options data
  source. A 403 cascade kills FII/DII data, options, fundamentals.
- **Likelihood: M** — NSE has aggressive bot detection; cookie/UA
  rotation is needed and must keep up with their changes.
- **Where**: `backend/app/services/nse_fetcher.py` (52% coverage —
  retry/backoff and 429 handling untested per `COVERAGE_REPORT.md`
  #10).
- **Mitigation**:
  - Cover retry/backoff branches.
  - Add circuit breaker — after N consecutive 403s, stop calling
    NSE for M minutes and degrade UI gracefully.
  - Fall back to BSE / NSDL / yfinance for the affected data class.
  - Long-term: paid market-data vendor (Truedata, GlobalDataFeeds).

## 8. Chrome Web Store rejection on permissions

- **Severity: M** — extension can't ship through the official channel.
- **Likelihood: M** — content script restricted to 12 IN-finance
  domains (good), but reviewers still scrutinise:
  - `host_permissions` scope.
  - The CSP that was added — make sure no `unsafe-eval`.
  - Any remote code execution path (lightweight-charts is bundled,
    confirm no CDN load).
  - Use of `storage` for API keys — must be `chrome.storage.local`
    or encrypted, not raw cookies.
- **Where**: `extension/manifest.json`,
  `extension/src/popup/*` build output.
- **Mitigation**:
  - Pre-submit dry-run via Chrome's manifest validator.
  - Privacy policy URL ready (data collection disclosure).
  - Justify each permission in the listing.
  - Have a fallback: side-loaded distribution for beta if the store
    rejects.

## 9. Defensive router imports masking real failures

- **Severity: M** — a typo in a new router silently disables a tab's
  backend; users see empty data, no error.
- **Likelihood: M** — `main.py:33-53` wraps `from app.routers import
  portfolio` in try/except. Useful during the swarm landing; a
  permanent foot-gun.
- **Where**: `backend/app/main.py:33-53`.
- **Mitigation**: convert to hard-fail at boot before Phase 1. If a
  router fails to import, the deploy fails — that is the correct
  behaviour. Add a CI smoke test that imports every router module.

## 10. In-process rate limiter incorrect under multi-worker

- **Severity: M** — limits are advisory at best. Abuse path:
  attacker exhausts LLM budget through repeated AI-analysis calls.
- **Likelihood: L today** (single worker). **H** the moment we
  scale to `--workers 2+`.
- **Where**: `backend/app/main.py:140-237` — module-level
  `_rate_buckets` dict.
- **Mitigation**: ADR-004 — Redis INCR+EXPIRE. **Hard prerequisite**
  for `--workers > 1` or replicas > 1.

---

## Honourable mentions (not in top 10 but worth tracking)

- **`market_regime.py` at 12% coverage** silently mis-weights
  recommendations under untested branches. Coverage debt with
  user-visible blast radius.
- **No DLQ for failed notifications** — repeated failures just log.
- **WS slow-consumer drops** logged only at disconnect — see ADR-005.
- **`google.generativeai` deprecated upstream** —
  `INTEGRATION_TODO.md` §2; migrate to `google.genai` before SDK is
  unsupported.
- **Backtester at 17% coverage** — any "this strategy made +X%"
  number we surface is barely tested.

## 11. Regulatory & Compliance risk of direct NSE/BSE scraping

- **Severity: H** — NSE/BSE do not provide public APIs; unauthorized scraping violates ToS and can result in legal action or IP blocks.
- **Likelihood: M** — Current implementation relies on `nse_fetcher` which mimics browser requests.
- **Where**: `backend/app/services/nse_fetcher.py`.
- **Mitigation**: Transition away from scraping. Integrate authorized Broker APIs (e.g., Zerodha Kite) or Authorized Information Vendors (AIVs) like TrueData as outlined in the scaling roadmap. Stop scraping if the application moves to public hosting.

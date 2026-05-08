# ADR-003 — Secrets at rest and multi-tenancy

Status: Accepted (interim) — **secrets-in-plaintext is acceptable for
single-user local install only.** Re-decision required before any
hosted deployment.
Date: 2026-05-08

## Context

Today the app is a single-user local install: the FastAPI process runs
on the user's machine, the SQLite file lives on their disk, the Chrome
extension talks to `localhost`. There is no notion of users, accounts,
or tenants in the schema.

Sensitive material currently stored in `settings` table
(`backend/app/database.py:42-47`, `backend/app/routers/settings.py`):

- `openai_api_key`, `gemini_api_key`, `claude_api_key`, `llm_api_key`
- (Implicit, used elsewhere): Telegram bot token, Twilio SID/auth token,
  SendGrid key, FCM server key, Kite API key/secret.

Storage is a plaintext `(key, value)` row. Read-side has been
hardened — `_redact_secrets` (`settings.py:35-55`) ensures values never
leave the process via the API. **At-rest is still plaintext on disk.**

Schema reality (`database.py`): no `user_id` / `tenant_id` column on
`watchlist`, `signals`, `price_alerts`, `portfolio_*`, `signal_outcomes`.
Single-tenant by accident, not by design.

## Decision

Two separable decisions:

### Secrets at rest

- **Now (single-user local):** keep plaintext in `settings`. File
  permissions on `agentdb.rvf` are the security boundary. Document this
  explicitly in the README so users know.
- **Hosted / shared deployment (≥ Phase 2 in roadmap):** move to
  **env-only for shared infrastructure secrets** (the *server's* LLM
  key, Twilio account, etc.) and **KMS-wrapped per-user columns** for
  per-user secrets (each user's broker OAuth token, their personal
  Telegram chat ID, etc.).

The KMS scheme (target):
- AWS KMS or GCP KMS data-key envelope encryption.
- New table `user_secrets(user_id, kind, ciphertext, dek_version, updated_at)`.
- `backend/app/services/secrets.py` (new) wraps encrypt/decrypt and
  caches DEKs in-memory with TTL.
- Rotate KEK quarterly; DEK on user trigger.

### Multi-tenancy

Introduce `user_id UUID NOT NULL` as a partition key on every
user-owned table when we add the second user. Specifically:
- `watchlist.user_id`
- `price_alerts.user_id`
- `signals.user_id` (only for user-specific signals — system signals
  stay user-agnostic)
- `portfolio_transactions.user_id`
- `signal_outcomes.user_id` (or scope dynamic weighting globally —
  decide at the time)
- `llm_usage.user_id` (per-user cap, not just global cap)
- New `users(id, email, created_at, ...)` table.

Authorisation: every router that reads/writes user-owned data must
filter by `user_id` derived from the authenticated principal — never
from a request parameter.

## Alternatives considered

### Option A — Status quo (plaintext, single user)
- Pro: zero work, ships today.
- Con: any second user, any cloud deploy, any backup leak = total
  credential compromise.
- Verdict: **acceptable only as long as the deployment is one user on
  one machine.** Block on any multi-user move.

### Option B — Env-only secrets, no DB storage
- Pro: simplest, no encryption code.
- Pro: works fine for *server-owned* secrets (one shared OpenAI key
  paid for by us).
- Con: doesn't work for *user-owned* secrets — Telegram chat IDs,
  broker OAuth tokens, per-user webhook URLs. Those have to be in
  the DB.
- Verdict: half the answer. Combined with KMS for the rest.

### Option C — KMS-wrapped column (chosen for hosted)
- Pro: industry-standard envelope encryption.
- Pro: rotation via re-encrypt, no DB schema change.
- Pro: KMS audit log shows who decrypted what, when.
- Con: adds KMS dependency, ~1ms per decrypt + ~$1/10K decrypts.
- Con: dev environments need a stub KMS or local key.
- Verdict: right answer for hosted. Adopt at Phase 2.

### Option D — HashiCorp Vault
- Pro: more flexible (dynamic secrets, leases, access policies).
- Con: heavier op burden, another service to run.
- Verdict: revisit at Phase 4 (platform), not before.

## Multi-tenancy implications

Things that break the moment a second user shows up:

1. **`settings` table is global.** A second user's risk_mode would
   overwrite the first's. → Move per-user knobs into a `user_settings`
   table; keep `settings` for *server* config only.
2. **Rate limiter keys on IP** (`main.py:227`). With NAT'd users on a
   single household IP, one user can rate-limit another. → Key on
   `(user_id, route)` once auth is in place.
3. **`signal_performance` is global.** That's actually fine — pattern
   performance is a market property, not a user property. Document it.
4. **`llm_usage` daily USD cap is global.** Needs to become per-user
   with a per-tenant ceiling.
5. **Orchestrator scans all watchlist symbols.** With N users, the
   scan blows up linearly. Need to deduplicate symbols across users
   and route alerts back to subscribers.
6. **`alert_checker` triggers all alerts.** With multi-tenant, must
   filter notification routing per `user_id`.
7. **WebSocket auth.** See ADR-002 — must identify the user, not just
   "a valid client".

## Consequences

Positive
- Acknowledging the gap explicitly is half the fight.
- Schema is small enough that adding `user_id` is a tractable migration.

Negative / debt
- Every router and service that touches user-owned data needs an audit
  before multi-tenancy ships. Estimate: 1–2 weeks of focused work plus
  schema migration window.
- `_redact_secrets` is the *only* current defence. If a future router
  bypasses it (e.g. a debug endpoint dumps `settings` directly), keys
  leak. Add a regression test that fuzzes `/api/**` for secret-shaped
  values in responses.

## Reversibility

- Adding `user_id` columns: **forward-only migration**. Backfill with
  a sentinel `00000000-…-default` for the existing single user before
  applying NOT NULL.
- KMS adoption: **two-way door** — you can decrypt back to plaintext
  if you change your mind (don't, but you can).

## Rollback

There is no useful rollback for "we leaked plaintext keys to a second
user". Therefore do not ship multi-tenant until both (a) `user_id`
columns exist with NOT NULL constraint and (b) KMS-wrapped columns are
in place for user-owned secrets.

## Open questions

- Do we want server-managed LLM (we pay) or BYO-key (user pays)? Affects
  whether per-user keys are even stored.
- For Indian retail users, KMS in `ap-south-1` (Mumbai) — confirm DPDP
  Act 2023 compliance with legal before hosting.

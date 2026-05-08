# ADR-004 — Rate limiter: in-process now, Redis later

Status: Accepted (interim)
Date: 2026-05-08

## Context

Current implementation: `backend/app/main.py:140-237`.

- `_rate_buckets: dict[str, list[float]]` — per-process module-level dict.
- Key: `f"{ip}:{bucket_id}"` where `bucket_id` is matched by ordered
  prefix+suffix table (`_ROUTE_RATE_LIMITS`) so that
  `/api/stocks/{sym}/technicals` no longer falsely inherits the AI
  analysis limit (this was a real bug, fixed pre-merge).
- Cleanup: every 100 requests, drops buckets idle for > 120s.
- IP extraction: `_client_ip()` — honours `X-Forwarded-For` only when
  `TRUST_FORWARDED=1` env is set, and only the leftmost *public* IP
  (`_is_public_ip` rejects private/loopback/link-local). Default: off,
  so XFF spoofing is not an attack vector when behind localhost.

Defaults:
- Per-route: ai_analysis 15/min, scan_trigger 5/min, screener 20/min,
  backtest 60/min.
- Global default: 120/min.

The author left an explicit warning at the top of `main.py:1-9`: this
is correct only with **one worker process**. With N workers, the
effective limit is N × configured.

## Decision

Keep the in-process limiter for now. Migrate to **Redis INCR + EXPIRE**
sliding window when *either* of:

1. We start running > 1 uvicorn worker (`--workers ≥ 2`), OR
2. We deploy > 1 replica behind a load balancer.

Whichever happens first. Treat this as a **hard prerequisite** to
horizontal scaling — multi-worker without distributed rate limiting
means our limits are advisory at best, broken at worst.

## Migration plan

Target shape (`backend/app/services/rate_limiter.py` — new file):

```python
async def allow(key: str, limit: int, window_secs: int) -> bool:
    """Sliding-window-via-fixed-window approximation using INCR+EXPIRE.

    Bucketise time into windows; key = f"rl:{key}:{epoch//window}".
    First INCR primes; if result == 1, set EXPIRE = window+1.
    Allow if INCR result <= limit.

    For stricter sliding window: ZADD timestamps + ZREMRANGEBYSCORE
    + ZCARD (Redis 6+). Slightly more expensive (~3 round trips).
    """
```

Wire into the existing `_check_rate_limit`. Keep the in-process path as
a fallback if Redis is unavailable — fail-open with a logged warning,
not fail-closed (the limiter is a guardrail, not security).

Order of operations:
1. Add `rate_limiter.py` with both backends (in-mem + Redis).
2. Switch `_check_rate_limit` to call `rate_limiter.allow()`.
3. Verify under load that p99 of `/api/health` doesn't regress > 2ms.
4. Flip `--workers 2`. Validate under load.
5. Remove the `_rate_buckets` module-level dict.

## Alternatives considered

### Option A — In-process dict (status quo)
- Pro: zero dependencies, fast (sub-ms).
- Pro: clean for local single-user install.
- Con: incorrect across workers/replicas.
- Verdict: keep for Phase 1 only.

### Option B — Redis INCR + EXPIRE (chosen)
- Pro: correct across workers/replicas.
- Pro: Redis already a dependency.
- Pro: ~1–2ms per check on local Redis, acceptable.
- Con: Redis becomes a hard dep — needs HA story (Sentinel/Cluster) to
  avoid taking down auth.
- Con: Lua script needed for atomic check-and-increment in stricter
  variants.
- Verdict: right answer at Phase 2.

### Option C — Sliding window via Redis ZSET
- Pro: actually-sliding window, not approximated.
- Pro: precise burst control.
- Con: 3x more Redis ops per request.
- Verdict: only if we get complaints about burst behaviour.

### Option D — API gateway rate limit (Kong, AWS API GW, Cloudflare)
- Pro: offloads from the app entirely.
- Pro: edge-level, blocks bots before they hit our infra.
- Con: per-route limits in the app give us business-logic flexibility
  (e.g. tighter limits for LLM-cost endpoints).
- Verdict: combine — edge for abuse, app for business limits. Phase 3.

## Consequences

Positive
- Clear graduation criteria: when workers > 1, switch.
- Existing `_match_route_limit` table is the right abstraction — Redis
  port is straightforward.

Negative
- Until we switch, *cannot* run > 1 worker. Document this in
  `start.sh` and any K8s manifest (HPA min/max replicas = 1).
- Fail-open behaviour means a Redis outage *removes* rate limiting
  rather than locking everyone out. Acceptable for a guardrail; if we
  ever use rate limit for security (per-user quota enforcement), this
  must become fail-closed.

## Reversibility

**Two-way door.** The `rate_limiter.allow()` interface hides the
backend. Switch by config.

## Open questions

- Per-user limits once auth is per-user (today: per-IP). Should the
  bucket key change to `(user_id, route)` once we have users? Yes —
  see ADR-003. Multi-tenancy work and rate limiter migration should
  happen in the same release.
- Do we want **token-bucket** semantics (burst-friendly) or strict
  sliding window? Token bucket is friendlier for legitimate burst use
  (e.g. dashboard refresh fires 6 requests in 200ms). Recommend token
  bucket via Redis Lua.

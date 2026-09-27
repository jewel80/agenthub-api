# Runbook: Redis down / rate-limit & lockout anomalies

Covers the failure modes introduced by the Redis-backed rate limiting, daily
token quota, and login lockout (roadmap §5, M1/M2). No credentials in this
file — see `.env.example` for the `REDIS_URL` placeholder.

## 1. Redis is down or unreachable

**Symptom:** logs show `redis unavailable, degrading: <ExceptionType>` at
WARNING from `agenthub.redis` (`app/core/redis.py`).

**Impact:**
- Per-user chat rate limiting, daily token quota, and login rate limiting
  fall back to **in-process, single-instance** state
  (`app/services/rate_limiter.py::HybridRateLimiter` /
  `TokenQuotaLimiter`). Limits are still enforced, but only per instance —
  running 2+ instances behind a load balancer means the *effective* limit is
  roughly `N_instances × configured limit` until Redis recovers.
- Nothing crashes and no request fails because of the Redis outage itself
  (verified by `tests/test_redis_limiter.py::test_degradation_when_redis_down`
  and the M3 cache-service tests once §2 lands — see `docs/PROGRESS.md`).

**What to do:**
1. Check Redis reachability: `redis-cli -u "$REDIS_URL" ping`.
2. Restart/recover the Redis service (managed Redis: check the provider
   dashboard; local dev: `docker compose -f docker-compose.dev.yml up -d`).
3. No app restart is required — `app/core/redis.py` retries lazily on the
   next call and clears its "unavailable" flag as soon as a command succeeds.
4. If running multiple instances and you need the *shared* limit enforced
   immediately (not per-instance), temporarily lower `RATE_LIMIT_PER_MIN` /
   `DAILY_TOKEN_QUOTA_DEFAULT` until Redis is back.

## 2. Login lockout stuck / user reports "too many attempts"

`app/services/login_guard.py` is **in-process only** (not Redis-backed) and
locks a given `(email, agent)` key with exponential backoff (30s → 60s → ...
capped at 15 minutes), self-clearing on a subsequent successful login or once
the lock expires.

**What to do:**
- **Wait it out** — the lockout is capped at 15 minutes and always expires
  on its own; there is no persistent/DB-backed lock to clear.
- If the deploy runs multiple instances, the lockout is **per-instance** —
  the user may succeed on a retry that lands on a different instance. This
  is a known limitation, not a bug (see README "Known limitations").
- A rolling restart of the affected instance clears all in-process lockout
  state immediately (use only if a user is genuinely blocked and cannot wait).

## 3. Verifying the fallback yourself

```bash
# with Redis up: normal Redis-backed behavior
docker compose -f docker-compose.dev.yml up -d
curl -i http://localhost:8000/agents/<slug>/login -d '...'   # X-RateLimit-* headers present

# simulate Redis down: point REDIS_URL at nothing, restart the app
REDIS_URL=redis://localhost:6399/0 uvicorn app.main:app --port 8000
# same endpoints still respond and still enforce limits (in-process now)
```

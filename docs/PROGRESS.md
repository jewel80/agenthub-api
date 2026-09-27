# AgentHub Progress

## Current milestone
M3 — Scale doc "Now" items. §2 `CacheService` ✅ complete, §1 read/write
split ✅ complete, §3 transactional outbox ✅ complete. Next: §5.1 `pg_trgm`.

## How this was verified (M0 audit, 2026-09-25/26)

```bash
git grep -niE "sqlite|aiosqlite"                 # empty (docs/comments only, no code path)
git grep -niE "password|postgresql://|sk-ant"    # placeholders / hashed-password code only
grep -n "sqlalchemy.url" alembic.ini             # blank
alembic downgrade base && alembic upgrade head   # clean on the dev DB (see note below)
pytest -q                                        # 104 passed, 4 skipped (live-Redis tests; no local Redis)
ruff check app alembic tests scripts             # all checks passed
ruff format --check .                            # 57 files "would reformat" — see Decisions
```

Backfill (fix-doc F1 step 2) was verified two ways:
1. `tests/test_migration_backfill.py` (new): downgrades the test DB below the
   `seq` migration, inserts 6 raw rows sharing one `created_at` with `id`
   order scrambled relative to conversation order, re-runs `alembic upgrade
   head`, and asserts the resulting `seq` order matches the migration's own
   tiebreak formula (`created_at ASC, role='user' DESC, id ASC`) exactly.
2. Live smoke test (see below) — normal chat flow through `/agents/{slug}/chat`
   several times, `GET history` returns strict `user, assistant, user, ...`.

**⚠️ Incident during audit:** step "alembic downgrade base && upgrade head"
was run without `ALEMBIC_DATABASE_URL` set, so it targeted `DATABASE_URL`
(the local dev DB) instead of a disposable database and wiped its
`agents`/`users`/`messages` rows. Recovered immediately via
`python -m app.pipeline.seed_agents` (100 agents / 400 sub-agents restored,
matches fix-doc §1.4's expected count). No `users`/`messages` data was lost
beyond this session's own local test data. Logged here per "report outcomes
faithfully"; no production/shared database was involved. **Lesson applied:**
all further downgrade/upgrade verification in this project uses pytest's
`test_migration_backfill.py`, which pins `ALEMBIC_DATABASE_URL` to
`TEST_DATABASE_URL` for the whole session (see `tests/conftest.py`).

Live endpoint smoke test (fresh `uvicorn`, `LLM_PROVIDER=mock`, against the
real dev Postgres DB, port 8123, torn down after): `/health` 200,
`/health/ready` 200, `GET /agents` 200, `GET /agents?q=doctor` 200 (SQL
filter), `GET /industries` 200, `GET /agents/{slug}` 200, `GET
/agents/nope` 404, `POST signup` 201, `POST signup` (unknown agent) 404,
`POST login` 200, `POST login` (wrong password) 401, `GET /me` 200 / 401
(no token), `POST chat` 200, `GET history` 200 (ordered), `POST chat`
cross-tenant 403, `POST /v1/.../chat/stream` 200 SSE (`start`/`delta`/`done`
observed), `GET /meta/usage` 200 (dev; would 404 in production without
`X-Admin-Token` — see F13 row).

## Audit table — doc #1 (Fix Instructions)

| ID | Item | Status | Commit | Test(s) | Notes |
|----|------|--------|--------|---------|-------|
| §1 | PostgreSQL-only setup | ✅ verified | `4a063af` | `test_config_guard.py` | No SQLite path; `DATABASE_URL` required, validated `postgresql+asyncpg://`; live-hit all endpoints against real Postgres |
| F1 | Conversation ordering (`seq`) | ✅ verified | `cfff140`, `4cd542e`, + this session's `test_migration_backfill.py` | `test_ordering.py`, `test_migration_backfill.py` | Backfill formula verified directly (see above), not just via app-level tests |
| F2 | LLM failures → 503 + Retry-After | ✅ verified | `cd1a718` | `test_llm_failures.py` | Timeout/connection/rate-limit/5xx all mapped; user turn survives, no assistant row on failure |
| F3 | JWT secret / CORS wildcard guards | ✅ verified | `d3eb52a` | `test_config_guard.py` | Raises at settings construction outside `development`; `ENVIRONMENT` actually used |
| F4 | Deactivated agents → 404 everywhere | ✅ verified | `1daba16` | `test_inactive_agents.py` | Shared `get_active_*_or_404` helper used by auth, agents router, chat engine |
| F5 | Signup race (`IntegrityError`) | ✅ verified | `5c32e0a` | `test_signup_race_and_readiness.py` | Rollback → re-select → idempotent path |
| F6 | `/health/ready` | ✅ verified | `28d901e` | `test_signup_race_and_readiness.py` | 503 on DB-down (verified live too); `render.yaml` points here |
| F7 | Composite history index | ✅ verified | `cfff140` (same migration as F1), test in `4cd542e` | migration test | `ix_messages_history(user_id, agent_id, sub_agent_id, seq)` |
| F8 | Catalog filtering/paging in SQL | ✅ verified | `61079ea` | `test_catalog_and_ratelimit.py` | `q` via `ILIKE`, industry case-insensitive, `limit`/`offset`, default unchanged |
| F9 | `Retry-After` on 429; resolve-before-limit | ✅ verified | `a4c884b` | `test_catalog_and_ratelimit.py` | 404/403 no longer consume budget; verified in both `/chat` and `/chat/stream` |
| F10 | Malformed JWT `sub` → 401 | ✅ verified | `a4c884b` | `test_catalog_and_ratelimit.py` | `uuid.UUID()` wrapped in try/except |
| F11 | Locked dependencies | ✅ verified, refreshed | `cfaa265` + this session | — | `requirements.lock` regenerated to include `redis` (added for M1); CI installs from the lock |
| F12 | JWT `agent_id` claim vs. `user.agent_id` | ✅ verified | `a4c884b`, `4cd542e` | `test_p3_hardening.py` | Mismatch → 401 in `get_current_user` |
| F13 | Protect `/meta/usage` in production | ✅ verified | `16eaa54` | `test_p3_hardening.py` | `X-Admin-Token`; empty token in prod → 404; verified 200 in dev live |
| F14 | Signup 409 policy | ✅ as designed (no code change needed) | — | `test_auth.py` | Kept for now per fix-doc instruction |
| F15 | Request-ID + structured logs | ✅ verified | `16eaa54` | `test_p3_hardening.py` | `X-Request-ID` accepted/generated, in every log line + error body; `LOG_FORMAT=json` option |
| F16 | Non-root container + migrations as release step | ✅ verified | `ff3ec33` | — | `Dockerfile` non-root `USER`; `docker-entrypoint.sh` runs migrate as separate step; documented in render.yaml |
| F17 | Refresh-token design note | ✅ (design-only, as scoped) | `e28f8f4` | — | Note lives with the auth service; implementation is roadmap §10 (not yet built — see M5) |
| F18 | Remove `python-multipart` unless needed | ✅ verified | n/a (never added) | `git grep -n multipart` → no hits | Nothing to remove |
| §6 | Tests & CI on Postgres | ✅ verified, extended | this session | `.github/workflows/ci.yml` | Added a `redis:7-alpine` service (port 6380) so the roadmap-§5 live-Redis tests run in CI instead of skipping |

**Doc #1 §7 Definition of Done: all items ✅.** 104 tests passing, 4 skipped
locally only because no local Redis is running (they run in CI now).

## Audit table — doc #2 (Improvement Roadmap), Phase 1

| ID | Item | Status | Commit | Test(s) | Notes |
|----|------|--------|--------|---------|-------|
| §3 | Chat streaming (SSE) | ✅ verified | this session | `test_streaming.py` (10 tests) | Fixed contract (`start`/`delta`*/`done`\|`error`), heartbeat, disconnect→`interrupted`, mid-stream error→`failed`, zero-text failure persists nothing, `STREAMING_ENABLED` toggle, concurrent-stream cap |
| §3.3.6 | `messages.status` migration | ✅ verified | this session | migration + `test_streaming.py` | Expand-only, `server_default='complete'`, check constraint |
| §4.1 | Anthropic prompt caching | ✅ verified | this session | covered indirectly (usage fields threaded through); no live Anthropic call in CI (mock provider) | System prompt + history-prefix breakpoints; `LLM_PROMPT_CACHE_ENABLED` toggle; current Anthropic minimum cacheable length (per SDK docs, ~1024 tokens for Haiku-class models) — **not independently re-verified against live Anthropic docs this session**, flagged below |
| §5 | Redis rate limiting | ✅ verified, 1 bug fixed against real Redis (see "Corrections" below) | this session (+ fix in M3) | `test_redis_limiter.py`, `test_rate_limiter.py` | Sliding-window Lua script, `X-RateLimit-*` headers, Redis-down degrades to in-memory |
| §5 | Daily token quota | ✅ verified | this session | `test_quota_and_lockout.py` | 429 + `Retry-After`; enforced before both `/chat` and `/chat/stream` |
| §5 | Login lockout (exponential) | ✅ verified | this session | `test_quota_and_lockout.py` | Per (email, agent); 30s→60s→...→15min cap |
| §5 | Login stricter rate limit | 🔧 fixed in review (was declared in settings, never wired) | this session | `test_login_rate_limit_is_stricter_than_lockout` | `LOGIN_RATE_LIMIT_PER_MIN` now enforced on `/login` (not `/signup` — see Decisions) |
| §5 | Global LLM concurrency cap | 🔧 fixed in review (only guarded non-streaming `/chat`) | this session | `test_stream_respects_global_llm_concurrency_cap` | Now also held for the duration of `stream_turn` |
| §5 | Move usage counters off in-process | ✅ verified | this session | `test_quota_and_lockout.py` | Token quota is Redis-backed (with in-process fallback) |

**Doc #2 §18 Definition of Done, Phase 1 items: all ✅** (Phase 2–4 items not started — see Needs human action / next milestones).

## Audit table — doc #3 (Scale Architecture), M3

| ID | Item | Status | Commit | Test(s) | Notes |
|----|------|--------|--------|---------|-------|
| §2 | `CacheService` (L1 in-process + L2 Redis, cache-aside) | ✅ verified | this session | `test_cache_service.py` | `app/services/cache.py`; only entry point talking to Redis for caching |
| §2.3.2/.3 | Versioned keys (`agenthub:{env}:{ns}:v{ver}:{ident}`); invalidation = `INCR`, no `KEYS`/`SCAN` | ✅ verified | this session | `test_hit_miss_invalidate_cycle` | Fixed a real bug during dev: Redis `INCR` on a missing key starts at 1, same as the "never invalidated" default, so the very first `invalidate()` was a no-op — changed the default to 0 (see Decisions) |
| §2.3.5 | TTL jitter | ✅ implemented | this session | — | `CACHE_TTL_JITTER_PCT`, ± spread on write |
| §2.3.6 | Stampede protection (short `SET NX` lock, ≤200ms poll) | ✅ verified | this session | `test_stampede_50_concurrent_misses_one_load` | 50 concurrent misses → 1 DB load |
| §2.3.7 | Stale-while-revalidate | ✅ verified against real Redis (fakeredis smoke check first, then re-confirmed once local Redis became reachable) | this session | manual smoke script (not committed) | Envelope stores `fresh_until`; Redis TTL outlives it by up to 60s so a stale value is served immediately while a background task refreshes |
| §2.3.8 | Negative caching | ✅ verified | this session | `test_negative_caching_avoids_repeat_loads` | Unknown/deactivated agent slugs cache a `None` result for `CACHE_NEGATIVE_TTL_SECONDS` |
| §2.3.9/.10 | L1 in-process (≤30s TTL) + cross-instance invalidation via Redis pub/sub | ✅ verified | this session | `test_invalidation_message_clears_l1_for_that_namespace`, `test_invalidate_publishes_to_the_cross_instance_channel` | Listener wired into `app/main.py` lifespan; degrades to a no-op loop (retries every 5s) when Redis is unavailable |
| §2.3.11 | Serialize Pydantic DTOs, never ORM objects | ✅ verified | this session | — | Used stdlib `json` + `model_dump_json`/`model_validate` rather than `orjson`/`msgpack` — no perf requirement measured yet; easy to swap later (see Decisions) |
| §2.3.12 | Redis error → degrade to DB; timeout ≤200ms; circuit breaker | ✅ verified | this session | `test_redis_down_still_returns_correct_data` | Reused the existing `app/core/redis.py` timeout/circuit-breaker (`is_unavailable()` added so the stampede-wait loop skips its ~200ms poll during a known outage instead of adding latency per request) |
| §2.4 | What to cache: catalog list, agent detail, industries | ✅ verified | this session | live smoke test (see below) | User-lookup caching (60s) and agent-chat-config caching are deferred — no runtime endpoint mutates users/agent-config yet outside the seed pipeline and auth's own DB reads, so there's no cache-invalidation trigger to wire up honestly yet; flagged under Needs human action |
| §2.5 | HTTP `ETag` + `Cache-Control: public, max-age=60, stale-while-revalidate=300`; `If-None-Match` → `304`; authenticated → `private, no-store` | ✅ verified | this session | live smoke test | `GET /agents`, `/agents/{slug}`, `/industries` — confirmed live: 200 with ETag, 304 on repeat with `If-None-Match`, `GET /me` (authed) → `Cache-Control: private, no-store` |
| §2.4 | Cache invalidation trigger | ✅ verified | this session | — | `python -m app.pipeline.seed_agents` invalidates `catalog`/`agent`/`industries` once after all upserts (not per-row, to avoid version churn) |
| §2.7 | Optional semantic LLM cache | 🕒 deferred | — | — | Off by default per spec; no FAQ-style agent exists in this catalog (all are professional-advisor personas) — no business trigger yet |
| §2.8 | Tests: hit/miss/invalidate, stampede, Redis-down, L1 cross-instance | ✅ verified against real Redis | this session | `tests/test_cache_service.py` — all 6 pass live (initially 2 pass/4 skip — no local Redis then; re-run against real Redis once available, 0 skips now) | Initial dev-time verification used a temporary `fakeredis` smoke script (not committed) to catch bugs the skip-when-unreachable tests couldn't yet — the version-fallback bug above was caught this way. Once real Redis became reachable, a genuine test-isolation bug surfaced (see "Corrections" below) and was fixed |
| §1.2.1/.2 | Two engines/session factories (`engine`/`AsyncSessionLocal` writer, `reader_engine`/`ReaderSessionLocal` reader) | ✅ verified | this session | `tests/test_read_write_split.py` | `app/core/db.py`; reader targets `DATABASE_READ_URL` only when `DB_READ_REPLICA_ENABLED` + set, else the primary |
| §1.2.3 | Reader is read-only (`SET TRANSACTION READ ONLY` via `default_transaction_read_only=on`) | ✅ verified against the real test DB | this session | `test_reader_session_rejects_an_insert` | A misrouted write raises `DBAPIError` immediately; new runbook: `docs/runbooks/read-write-split.md` |
| §1.2.4 | Routing table applied | ✅ verified | this session | `test_catalog_endpoint_reads_still_work_with_replica_disabled` + full suite | Catalog (`GET /agents`, `/agents/{slug}`, `/industries`) → `get_read_db`; `GET history` → `get_read_db_for_user` (read-your-writes aware); signup/login/chat writes → unchanged (`get_db`, the writer) |
| §1.2.5 | Read-your-writes (Redis flag, TTL = `READ_YOUR_WRITES_WINDOW_SECONDS`; Redis down → writer) | ✅ verified against real Redis | this session | `test_read_your_writes_marks_and_expires`, `test_read_your_writes_fails_safe_when_redis_down`, `test_read_your_writes_moot_without_a_replica` | `mark_read_your_writes` called after both the user-turn and assistant-turn commits in both the non-streaming and streaming chat paths (`app/services/chat_engine.py`); a no-op while no replica is configured (nothing to protect against) |
| §1.2.6 | Replica lag guard (checks every 5s, marks unhealthy above `DB_REPLICA_MAX_LAG_SECONDS` or on error) | ✅ implemented; ⛔ not exercised against a real replica (none exists in this environment) | this session | `test_unhealthy_replica_routes_to_primary` (sentinel-based routing-logic test) | `run_replica_lag_guard()` wired into `app/main.py` lifespan; a no-op loop when no replica is configured (the default) — flagged under Needs human action for real-replica validation |
| §1.3 | "Replica disabled ⇒ every endpoint behaves exactly as before" | ✅ verified | this session | full suite (127 passed) + live smoke test | Catalog/history endpoints hit live via `uvicorn` (mock LLM) after the change: `/agents` 200, `/agents/{slug}` 200, chat write 200, history immediately shows both turns |
| §3 pt.1 | `outbox_events` table + partial index for the poll query | ✅ verified | this session | `test_event_written_iff_transaction_commits`, `test_claim_batch_locks_unpublished_oldest_first` | `app/models/outbox.py`; added a `seq` identity column beyond the doc's literal schema — see Decisions (created_at collides within one transaction, same as `messages.seq`) |
| §3 pt.2 | Event written in the same transaction as the business change | ✅ verified | this session | `test_event_written_iff_transaction_commits` (rollback ⇒ never written; commit ⇒ durably written) | Wired into `auth_service.signup` (`user.signed_up`), `chat_engine.run_turn`/`_persist_streamed_turn` (`chat.completed`), `seed_agents.seed` (`agent.updated`) |
| §3 pt.3 | Relay worker: `FOR UPDATE SKIP LOCKED` batch → Redis Streams → mark published | ✅ verified against real Redis | this session | `test_relay_publishes_exactly_once_per_event`, `test_claim_batch_locks_unpublished_oldest_first` | `app/services/outbox_relay.py`; wired into `app/main.py` lifespan as a background task, polls every `OUTBOX_RELAY_INTERVAL_SECONDS` |
| §3 pt.4 | Idempotent consumers (dedupe on event id); retry with backoff; dead-letter + alert after `OUTBOX_MAX_ATTEMPTS` | ✅ verified against real Redis | this session | `test_consumer_is_idempotent_on_a_duplicate_delivery`, `test_consumer_leaves_failed_handler_unacked_for_redelivery`, `test_relay_moves_to_dlq_after_max_attempts` | `app/services/outbox_consumer.py` (consumer-group read + `SET NX` dedupe) + `app/services/outbox_relay.py` (DLQ move + `logger.error` as the "alert" — no PagerDuty/Sentry yet, that's roadmap §9); one real consumer built (`chat.completed` → durable usage counter, `app/services/outbox_handlers.py`) — see Decisions re: why not one per event type |
| §3 pt.5 | Initial events: `user.signed_up`, `chat.completed`, `message.feedback`, `agent.updated` | ✅ 3 of 4 wired; 🕒 `message.feedback` deferred | this session | — | No feedback endpoint exists in this API yet — nothing to emit the event from; not fabricated. `agent.updated` is emitted but cache invalidation stays synchronous in the pipeline (see Decisions) |
| §3 pt.6 | Cleanup job: delete published events older than the retention window | ✅ verified | this session | `test_cleanup_deletes_only_old_published_events` | `scripts/cleanup_outbox.py`; no scheduler wired up to run it periodically yet (none exists in this project) — flagged under Needs human action |

## Corrections (found once real Redis became reachable)

A local Redis became reachable this session for the first time (previously
this machine had no Docker and every Redis-dependent test skipped — see
Needs human action). Full suite: **120 passed, 0 skipped** (up from
110 passed / 8 skipped). Re-running the whole Redis-dependent surface for
real, instead of the skip-when-unreachable path, surfaced three real issues
— reported faithfully rather than silently fixed and left undocumented:

1. **`localhost` vs `127.0.0.1` (environment, not app code).** On this
   Windows machine, Python's `getaddrinfo("localhost", ...)` resolves to
   `::1` (IPv6) first; the local Redis server only listens on IPv4, so
   `redis://localhost:6380/0` timed out instead of connecting. Fixed by
   using `127.0.0.1` explicitly everywhere a test/`.env.example`/compose
   file names the local Redis: `tests/test_redis_limiter.py`,
   `tests/test_cache_service.py`, `.env.example`, `app/core/config.py`
   (comment), `docker-compose.dev.yml` (comment), `README.md`. This is why
   the 4 `test_redis_limiter.py` tests and 4 `test_cache_service.py` tests
   were "skipped" rather than failing outright — `ping()` genuinely
   couldn't reach the server, which is exactly the condition those fixtures
   are designed to skip on.
2. **Real bug in `RedisRateLimiter.hit()`** (`app/services/rate_limiter.py`,
   roadmap §5, predates this session — not part of the CacheService work):
   the Lua script's third return value is overloaded (remaining count when
   allowed, reset-seconds when blocked), but the Python side always treated
   it as `limit - 1` when allowed (ignoring the actual `used` count) and
   never zeroed `reset_after` for the allowed case. `X-RateLimit-Remaining`
   was therefore wrong on every allowed request once more than one request
   had been made in the window, and `X-RateLimit-Reset` leaked a bogus
   value on success. This was never caught locally because the test that
   would have caught it (`test_redis_rate_limiter_blocks_at_limit`) always
   skipped here for lack of Redis, and (as far as this session can tell)
   this branch had not yet been pushed for CI to run it either. Fixed to
   read the script's third value correctly per the `allowed` flag; test now
   passes against real Redis.
3. **Test-isolation bug in `tests/test_cache_service.py`** (this session's
   own new file): tests used fixed namespace names (`"t-cache-1"`, etc.)
   and only cleared the namespace's *version* key before each run. Real
   Redis persists data across separate `pytest` invocations (unlike the
   ephemeral `fakeredis` used for initial dev-time verification), so a key
   written by an earlier run — still within its TTL — was read back as a
   false "cache hit", making the loader appear to not run
   (`assert calls == 1` failing with `calls == 0`). Fixed by giving each
   test a fresh UUID-suffixed namespace (`_fresh_namespace()`), which
   can't collide with any prior run's data, rather than trying to
   enumerate and delete every versioned key by hand.
4. **Real bug in `app/core/redis.py`, found while building/re-verifying M3
   §1's read-your-writes tests**: `_build_client()` used the same 0.2s
   timeout for both `socket_timeout` (per-command latency, the thing
   roadmap §5 / scale §2.3.12 actually mean by "timeout ≤ 200ms") and
   `socket_connect_timeout` (establishing a brand-new TCP connection). The
   cache's 50-concurrent-miss stampede test intermittently failed
   (`calls` as high as 37, not 1) because establishing 50 simultaneous new
   connections from a cold pool (this test's `live_redis` fixture closes
   and recreates the client every test) occasionally took a task longer
   than 200ms to connect — a real but *transient* condition, not an actual
   Redis outage — which tripped the shared `_unavailable` circuit breaker
   for every other concurrent task, making them all skip their stampede
   poll-wait and redundantly call the loader. Confirmed with a standalone
   repro script (10/10 clean before the fix would occasionally spike to
   6-37 calls; 10/10 clean after) before touching production code. Fixed
   by splitting the timeout: `socket_connect_timeout` is now 2.0s (room for
   a connection-establishment burst), `socket_timeout` stays 0.2s (a single
   slow command still can't stall a request). Re-ran the full suite 3x
   clean (127 passed) plus the stampede test in isolation 8x after the fix.

## Corrections (found while building/live-testing M3 §3 outbox)

Two more real bugs surfaced while building and live-smoke-testing the
outbox relay/consumer against real Redis — the app hung on the very first
signup/chat request during the live smoke test, which led to both:

5. **`call()` treated *any* `RedisError` as a full outage, including a
   perfectly benign `ResponseError`** (e.g. `BUSYGROUP Consumer Group name
   already exists` from a redundant `XGROUP CREATE`). Since `ResponseError`
   means "the server answered, this specific command failed for a domain
   reason" — not "Redis is unreachable" — this was needlessly tripping the
   shared `_unavailable` circuit breaker for *every* other Redis-backed
   feature (cache, rate limiting, read-your-writes) whenever the consumer
   loop made its second pass. Fixed in `app/core/redis.py::call()` to only
   trip the breaker on a real connection/timeout error, not a
   `ResponseError`; also made the consumer only call `XGROUP CREATE` once
   per stream per process instead of every poll (`_groups_ensured`), so the
   expected `BUSYGROUP` response stops happening on every pass regardless.
6. **`_unavailable` had no recovery path at all — a much bigger, pre-existing
   bug this exposed.** Once *anything* set `_unavailable = True`,
   `get_redis()` refused to hand out a client forever after: `if
   _unavailable: return None`, with nothing else in the module ever
   retrying to notice Redis came back. Combined with bug 5 above (which
   *shouldn't* have tripped the breaker at all, but did), the consumer's
   very first redundant group-creation call permanently disabled Redis for
   the whole process — not "degrades until Redis recovers" as the module's
   own docstring promised, but "degrades forever after the first hiccup,
   restart required." This affected every Redis-backed feature in the app,
   not just the outbox, and predates this session; M3 §1/§2's own tests
   never caught it because each test resets `_unavailable = False` directly
   in its fixture teardown rather than exercising real recovery. Fixed with
   a half-open-circuit-breaker pattern: `_unavailable_since` + a 5s
   cooldown, after which exactly one retry attempt is let through
   (`_should_skip_attempt()`); a success resets the breaker, a repeat
   failure restarts the cooldown. Verified with a standalone script
   (forced-unavailable → simulated cooldown elapsed → `ping()` succeeds and
   clears the flag).
7. **Root cause of the actual hang**: `outbox_consumer.py`'s `XREADGROUP
   ... BLOCK 2000` asked Redis to hold the connection for up to 2000ms
   waiting for new stream entries, but the shared client's own command
   socket timeout is 200ms — so the *client* aborted every blocking read
   as a timeout well before Redis could honor the block, tripping the
   breaker (bug 5) every single poll, and — with no sleep on the "nothing
   read" path in `run_consumer_loop` — retrying immediately in a tight
   loop. That busy-loop (compounded by bug 6 meaning it could never
   recover) is what hung the live smoke test's very first request: the
   consumer task starved the event loop of turns badly enough that the
   HTTP request handling coroutine never got to run. Fixed by dropping
   `_BLOCK_MS` to 50 (safely under the 200ms command timeout) and adding an
   explicit `_IDLE_SLEEP_SECONDS` (0.5s) pause in `run_consumer_loop` when a
   poll finds nothing — the block no longer doubles as the pacing
   mechanism. Re-verified live end to end (mock LLM, real Redis): signup,
   two chat turns, and the durable `chat.completed` usage counter all
   completed in 16-112ms with no errors in the log beyond the expected
   one-time `BUSYGROUP` warning.

## Decisions & assumptions (M3 §2)

- **Serialization is stdlib `json` + Pydantic, not `orjson`/`msgpack`.** The
  scale doc suggests either for perf; no latency/throughput requirement has
  been measured yet to justify the extra dependency. Easy to swap inside
  `_serialize`/`_deserialize` later without touching call sites.
- **User-lookup (60s) and agent-chat-config caching (§2.4 table) are not
  wired up yet.** Both need a real invalidation trigger (user update/
  deactivate/logout-all; agent-config update) and neither has a runtime
  mutation path yet — auth reads users directly per request and agent
  config is only ever changed via the seed pipeline (already invalidates
  the agent namespace). Wiring a cache with no invalidation trigger would
  be worse than not caching. Revisit when a user-profile-update or
  live-agent-config-edit endpoint exists.
- **Redis `INCR`-on-missing-key starts at 1** — same value the "no version
  yet" default originally returned, which made the very first
  `invalidate()` call after boot a silent no-op (old data kept matching the
  "new" key). Fixed by making the no-version-yet default `0` instead of
  `1`; caught via the fakeredis smoke check, not by the (locally-skipped)
  live-Redis test suite — see Needs human action re: local Redis.
- **SWR's background revalidate reuses the stampede lock pattern** (a
  separate `:revalidate-lock` key) so a slow loader doesn't get triggered
  by every concurrent stale read of the same key, not just true cache
  misses.

## Decisions & assumptions (M3 §1)

- **`get_db` is the doc's `get_write_db()`, kept under its existing name.**
  Renaming it would touch every write-path call site (auth.py, chat.py) for
  no functional benefit and risks an unrelated-looking diff across files
  outside this section's scope. Documented as the mapping in
  `app/core/db.py`'s module docstring.
- **Two engines exist even with the replica disabled.** The reader engine
  targets the *same* URL as the writer in that case, so it's a second
  connection pool to one database — accepted per the scale doc's own
  wording ("the code path stays the same; only the target changes") and
  because it's how the read-only enforcement gets tested now, before any
  real replica exists.
- **Read-your-writes is gated on `_replica_configured`, not just on
  Redis being reachable.** Without a replica, "the reader" already *is*
  the primary, so there is nothing to protect against — always marking/
  checking the flag would just add Redis round trips for zero benefit.
  This also means `READ_YOUR_WRITES_WINDOW_SECONDS`/`REDIS_URL` are inert
  until a replica is actually turned on; documented in the runbook.
- **`mark_read_your_writes` is called after *both* the user-turn and the
  assistant-turn commits** in every chat path (non-streaming and
  streaming), not just once — a client could plausibly fetch history in
  the gap between the two writes, and the window is only refreshed while
  a replica is actually configured, so the extra Redis call is free in the
  shipped (replica-disabled) default.
- **Fail-safe direction for read-your-writes when Redis can't be checked
  is "assume they wrote recently"** (route to the writer), not "assume they
  didn't" — mirrors the CacheService's own principle of failing toward
  correctness over the read-scaling optimization. Note the ordering
  subtlety this required: `redis.call()` returns `None` for both "key
  absent" and "Redis errored", so `is_unavailable()` must be checked
  *after* the call attempt (reflecting whether that specific call just
  failed), not before — checking it before would use its state as of the
  *previous* call, which the read-your-writes fail-safe test caught.
- **The replica lag guard (§1.2.6) is implemented but not exercised
  against a real replica** — none exists in this environment. It's a
  no-op loop while no replica is configured (the default), so this doesn't
  block anything; flagged under Needs human action for whoever turns on a
  real replica.

## Decisions & assumptions (M3 §3)

- **`seq` (identity column) added beyond the doc's literal schema.** The
  doc's table definition doesn't list one, but `created_at` collides for
  events written in the same transaction (Postgres `now()` is
  transaction-start time — identical rationale to `messages.seq`, already
  established in this codebase). Caught via a genuinely flaky test
  (`test_claim_batch_locks_unpublished_oldest_first`, order `[2, 1]` instead
  of `[1, 2]` on some runs) before it could reach the relay's real poll
  query. The partial index is on `seq` instead of `(created_at, id)`.
- **In-process background tasks (`asyncio.create_task`), not arq/Celery.**
  The doc says "Consumers (arq/Celery workers)" — read here as an example,
  not a hard requirement. Introducing a task-queue framework and a
  separate worker deployment is a bigger step than this session's "Now"
  scope justifies for one consumer; the relay/consumer loops follow the
  exact pattern already used for cache invalidation (§2) and the replica
  lag guard (§1). Revisit once volume or handler count justifies a
  dedicated worker fleet — the Streams-based interface underneath doesn't
  change either way.
- **`message.feedback` is not wired up.** No feedback endpoint exists in
  this API — there is nothing to emit the event from. Not fabricated just
  to check a box; revisit when/if a feedback endpoint is built.
- **`agent.updated` is emitted, but cache invalidation stays synchronous
  in `seed_agents.py`, not routed through the outbox consumer.** The seed
  pipeline is a rare, manual admin operation, not a hot request path — the
  "must not slow down or break the request" motivation for going async
  doesn't apply here, and correct-immediately is strictly better than
  eventually-consistent for a rarely-run admin script. The event is still
  emitted (in the same transaction as the upserts) so a *future* consumer
  (e.g. an audit log, or a notification to other services) has something
  to subscribe to without needing another code change.
- **`user.signed_up`'s payload omits the email** (only `user_id`/`agent_id`)
  — no consumer needs it yet (no welcome-email service exists), and
  Redis Streams don't necessarily share the primary DB's retention/access
  posture, so minimizing PII there by default seemed the safer call than
  including it "just in case."
- **Only one real consumer built (`chat.completed` → durable usage
  counter), not one per initial event type.** It's enough to prove and
  test the whole idempotent-consumer pattern honestly; a consumer with no
  real downstream effect (nothing yet reads `user.signed_up`/
  `agent.updated`) would just be inert scaffolding. The relay itself
  (publish to the stream) has independent value and is fully tested
  regardless of whether a consumer exists yet for a given event type.
- **No scheduler wired up for `scripts/cleanup_outbox.py`.** No cron/task
  scheduler infrastructure exists in this project yet; the script is ready
  to be invoked by whatever gets added later (a platform cron job, a CI
  scheduled workflow, etc.) — flagged under Needs human action.

## Out-of-scope findings

- `ruff format --check .` reports 57 files "would reformat". Root cause:
  `core.autocrlf=true` on this Windows checkout (git stores/serves CRLF)
  while `ruff format` emits LF; nearly every file in the repo shows this
  diff, unrelated to actual code style. CI only runs `ruff check` (not
  `format --check`), so this has never gated anything. **Not fixed** —
  reformatting ~57 files now would produce a huge diff unrelated to this
  work. Needs a deliberate decision (e.g. `.gitattributes` with `text=lf`
  for `*.py`, or drop `ruff format` from the workflow) rather than a
  drive-by fix.
- `app/pipeline/seed_agents.py`, `scripts/create_test_db.py`,
  `scripts/set_db_urls.py` are solid dev-ergonomics scripts already in the
  repo; not part of any doc's scope, left untouched.

## Decisions & assumptions

- **F14**: kept as-is (signup returns 409 on a wrong-password duplicate);
  fix-doc explicitly says to revisit only with email verification (roadmap
  §10, not yet built).
- **`LOGIN_RATE_LIMIT_PER_MIN` scope**: wired onto `/login` only, not
  `/signup`. The setting's own name/comment (`# per email+agent`) and the
  roadmap's framing ("brute-force protection") both target credential
  guessing; signup abuse is already covered by the per-IP middleware
  limiter and F5's race handling. Applying it to signup too would double
  count the same key across the two existing `test_login_success_clears_
  failure_history`-style tests and isn't what the setting was named for.
- **Global LLM concurrency cap on streaming**: chosen to hold the semaphore
  for the *entire* stream duration (including heartbeat waits), not just
  the call setup — that's when the upstream connection is actually open
  and consuming the provider's own concurrency budget.
- **Prompt-cache minimum length**: not re-verified against live Anthropic
  docs this session (no network fetch performed for this audit). Flagged
  under Needs human action.
- Dev database wipe/reseed (see incident note above) — recorded rather than
  hidden; no other data affected.

## Needs human action

- 📄 **Confirm current Anthropic prompt-cache minimum length** for the
  configured model (`claude-haiku-4-5`) against live docs — the
  implementation caches unconditionally when enabled; if the model's
  minimum is higher than typical short system prompts in the seed data,
  cache writes may no-op harmlessly but won't show a hit-rate win. No code
  change is blocking on this; it's a verification/tuning item.
- ✅ **Local Redis** — resolved this session. A Redis instance is now
  reachable at `127.0.0.1:6380` (this machine still has no Docker CLI, so
  it isn't necessarily the `docker-compose.dev.yml` container — whatever it
  is, it answers `PING` on that port). All 8 previously-skipped
  Redis-dependent tests (`test_redis_limiter.py`, `test_cache_service.py`)
  now run for real locally: 120 passed, 0 skipped. See "Corrections" above
  for what that surfaced. `docker-compose.dev.yml` remains the documented
  way to get one if this local instance ever goes away.
- 📄 **User-lookup and agent-chat-config caching (scale-doc §2.4)** are not
  wired up — see the M3 §2 Decisions above. Needs a real invalidation
  trigger (a user-update/deactivate endpoint, a live agent-config-edit
  endpoint) before it's worth adding; neither exists yet.
- 📄 **Replica lag guard (scale-doc §1.2.6) untested against a real
  replica** — this environment has no Postgres streaming replica to point
  `DATABASE_READ_URL` at. The guard and the unhealthy→primary fail-over are
  covered by a routing-logic unit test (sentinels), not an integration test
  against real replication lag. Before enabling `DB_READ_REPLICA_ENABLED`
  in any real deployment: provision the replica, point `DATABASE_READ_URL`
  at it, and re-verify `run_replica_lag_guard()` against real lag (e.g. by
  briefly pausing replication) rather than trusting the unit test alone.
- 📄 **No scheduler for `scripts/cleanup_outbox.py`** (scale-doc §3 point
  6) — no cron/scheduled-task infra exists in this project yet. The script
  is ready; wire it into whatever platform-level scheduler gets adopted.
- 📄 **Consider a real worker fleet (arq/Celery) once outbox volume/handler
  count grows** — see M3 §3 Decisions for why in-process `asyncio` tasks
  were used instead for now; the Streams-based interface is unaffected
  either way if this changes later.
- 📄 Everything under fix-doc §8 / roadmap "What I cannot do in code":
  managed Postgres failover, PgBouncer deployment, CDN/WAF, pen test, DR
  restore drill, secret-manager rotation, production load test, staging
  environment provisioning — none attempted, none claimed done.
- 📄 `ruff format --check` / CRLF decision (see Out-of-scope findings).

## Milestone log

- **M0 (Review Step 1)** — ✅ complete. Audited F1–F18 + §1/§6; found 1 real
  test-coverage gap (F1 backfill never proven against real pre-existing
  data) and fixed it. Everything else was already correctly implemented
  and committed by the prior session.
- **M1 (Scale prerequisites: Redis)** — ✅ complete. `app/core/redis.py`
  already existed (safe-degradation client); this session added it to
  `docker-compose.dev.yml` + CI and documented `REDIS_URL` in
  `.env.example`.
- **M2 (Roadmap Phase 1)** — ✅ complete. Streaming, prompt caching, and
  Redis rate limiting/quota/lockout were already implemented (uncommitted)
  by the prior session; this session verified them, found and fixed 2 real
  gaps (login rate limit unwired, concurrency cap not applied to streams),
  added the missing tests, and committed everything in atomic commits.
- **Safety hardening (this session, before M3)** — added
  `app/core/db_safety.py::refuse_if_main_db` (+ `tests/test_db_safety_guard.py`)
  so tests and migration-check scripts refuse to run whenever their target
  database name equals `DATABASE_URL`'s — the exact class of mistake behind
  the M0 incident above. `tests/conftest.py` and the new
  `scripts/verify_migration_cycle.py` (safe replacement for the ad hoc
  downgrade/upgrade cycle) both use it. `alembic/env.py` now prints the
  target database name (never credentials) before every migration command.
  Codified as `docs/CLAUDE_CODE_INSTRUCTIONS.md` §2.10.
- **Docs-update rule (this session, before M3)** — added a mandatory
  per-section docs checklist to `docs/CLAUDE_CODE_INSTRUCTIONS.md` §3
  (PROGRESS.md, source-doc DoD checkboxes, README, `.env.example`,
  runbooks — all in the same commit as the code). Applied retroactively to
  M0–M2: ticked the fix-doc §7 DoD (all 8 items, with
  "Implemented: `<commit>` — `<file/test>`" notes) and the roadmap §18 DoD
  (ticked only the 2 items that are actually fully done — Redis rate
  limiting/quota, no credentials in repo — and added "Partial:" notes on
  the others so nothing is claimed done that isn't). Refreshed README's env
  var table (was missing ~16 vars already present in `.env.example`) and
  API reference (missing the streaming endpoint), and added
  `docs/runbooks/redis-degradation.md` for the two failure modes M1/M2
  introduced (Redis down, login-lockout stuck state). `.env.example` was
  already complete for M0–M2 — no changes needed there.
- **M3 (Scale doc "Now" items: `CacheService`, read/write split, outbox,
  `pg_trgm`)** — in progress, one section at a time per §10's order.
  **§2 `CacheService` — ✅ complete this session**: multi-layer cache
  (in-process L1 + Redis L2), versioned-key invalidation, TTL jitter,
  stampede protection, negative caching, stale-while-revalidate,
  cross-instance L1 invalidation via pub/sub, wired into the public catalog
  endpoints with HTTP `ETag`/`304`/`Cache-Control`. See the doc #3 audit
  table above for the full per-item breakdown. Re-verified against real
  Redis once it became reachable this session (see "Corrections" above) —
  full suite now 120 passed, 0 skipped.
  **§1 Read/write session split — ✅ complete this session**: two
  engines/session factories (`get_db` writer, `get_read_db` reader);
  reader enforces `SET TRANSACTION READ ONLY` at the Postgres session
  level (misrouted writes fail loudly — proven against the real test DB);
  routing table applied (catalog + history → reader, signup/login/chat →
  writer unchanged); read-your-writes via Redis (`get_read_db_for_user`,
  wired into the history endpoint); replica lag guard implemented and
  wired into the app lifespan (no-op while disabled, the shipped default).
  While re-verifying this against real Redis, found and fixed a real bug
  in `app/core/redis.py` (connect-timeout too tight for a 50-connection
  burst — see "Corrections" above). Full suite: 127 passed, 0 skipped,
  reproduced clean 3x in a row. Live-verified end to end (mock LLM):
  catalog reads, a chat write, and history showing both turns immediately
  after.
  **§3 Transactional outbox — ✅ complete this session**: `outbox_events`
  table (with a `seq` identity column, not in the doc's literal schema —
  see Decisions), events written in the same transaction as
  `user.signed_up`/`chat.completed`/`agent.updated`, a relay worker
  (`FOR UPDATE SKIP LOCKED` → Redis Streams → mark published, with
  dead-lettering after `OUTBOX_MAX_ATTEMPTS`), and one real idempotent
  consumer (`chat.completed` → a durable, cross-instance usage counter,
  addressing a limitation `observability.py` had already flagged). While
  live-smoke-testing this, the app hung on the very first request — traced
  to a genuine chain of 3 bugs (a `ResponseError` wrongly tripping the
  Redis circuit breaker, that breaker having no recovery path at all once
  tripped, and a consumer `BLOCK` duration exceeding the client's own
  command timeout) — all fixed and documented in "Corrections" above, with
  a clean live re-run afterward (signup + 2 chat turns + durable counter,
  16-112ms each, no errors). Full suite: 136 passed, 0 skipped, reproduced
  clean 3x. Next: §5.1 `pg_trgm`.

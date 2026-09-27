# AgentHub Progress

## Current milestone
M2 — Roadmap Phase 1 (✅ complete) → about to start M3 (Scale doc "Now" items)

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
| §5 | Redis rate limiting | ✅ verified | this session | `test_redis_limiter.py`, `test_rate_limiter.py` | Sliding-window Lua script, `X-RateLimit-*` headers, Redis-down degrades to in-memory |
| §5 | Daily token quota | ✅ verified | this session | `test_quota_and_lockout.py` | 429 + `Retry-After`; enforced before both `/chat` and `/chat/stream` |
| §5 | Login lockout (exponential) | ✅ verified | this session | `test_quota_and_lockout.py` | Per (email, agent); 30s→60s→...→15min cap |
| §5 | Login stricter rate limit | 🔧 fixed in review (was declared in settings, never wired) | this session | `test_login_rate_limit_is_stricter_than_lockout` | `LOGIN_RATE_LIMIT_PER_MIN` now enforced on `/login` (not `/signup` — see Decisions) |
| §5 | Global LLM concurrency cap | 🔧 fixed in review (only guarded non-streaming `/chat`) | this session | `test_stream_respects_global_llm_concurrency_cap` | Now also held for the duration of `stream_turn` |
| §5 | Move usage counters off in-process | ✅ verified | this session | `test_quota_and_lockout.py` | Token quota is Redis-backed (with in-process fallback) |

**Doc #2 §18 Definition of Done, Phase 1 items: all ✅** (Phase 2–4 items not started — see Needs human action / next milestones).

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
- 📄 **Local Redis for full local test coverage**: this machine has no
  Docker installed (Docker Desktop appears partially uninstalled — an
  orphaned stopped `docker-desktop` WSL distro remains, no `docker` CLI on
  PATH). `docker-compose.dev.yml` is ready (`redis:7-alpine` on host port
  6380); once Docker is available, `docker compose -f docker-compose.dev.yml
  up -d` unlocks the 4 currently-skipped `test_redis_limiter.py` tests
  locally. CI already runs them (Redis service container added this
  session).
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
  `pg_trgm`)** — starting now, one section at a time per §10's order:
  §2 `CacheService` → §1 read/write split → §3 outbox → §5.1 `pg_trgm`.

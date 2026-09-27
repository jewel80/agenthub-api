# AgentHub API — Production Fix Instructions

> **Audience:** the developer / coding agent implementing the fixes
> **Source:** Production Readiness Review (commit `bdc2c4e`) — verdict **Conditional No-Go**
> **Goal:** clear all P0 + P1 items, land P2 hardening, and run the API on **PostgreSQL** for a supervised single-instance pilot.

---

## 0. Ground Rules (read first)

1. **Database = PostgreSQL only.**
   - Use the async driver: `postgresql+asyncpg://...`
   - Remove any SQLite fallback / default URL from the code. If `DATABASE_URL` is missing, the app must **fail at startup** with a clear error.
   - Tests also run against PostgreSQL (a separate test database), not SQLite.
2. **No credentials in code — ever.**
   - Database credentials will be provided separately. Read them **only** from environment variables (`.env` locally, platform secrets in deploy).
   - Do not hardcode host, user, password, or DB name in any `.py`, `alembic.ini`, `render.yaml`, Dockerfile, test, or CI file.
   - `.env` stays gitignored. Update `.env.example` with **placeholders only**.
3. **Every schema change ships as an Alembic migration.** No `create_all()` in production paths.
4. **Every fix gets a test.** A finding is not "done" until a test proves it.
5. **Do not change the public response contract** unless a step below says so.
6. Work in the order given (P0 → P1 → P2 → P3). One PR/commit per finding ID (e.g. `fix(F1): ...`).

---

## 1. PostgreSQL Setup

### 1.1 Environment variables

Add/confirm these in `app/core/config.py` (pydantic settings) and in `.env.example`:

```dotenv
# .env.example — placeholders only, real values provided separately
ENVIRONMENT=development            # development | staging | production
DATABASE_URL=postgresql+asyncpg://<USER>:<PASSWORD>@<HOST>:<PORT>/<DB_NAME>
TEST_DATABASE_URL=postgresql+asyncpg://<USER>:<PASSWORD>@<HOST>:<PORT>/<TEST_DB_NAME>
DB_SSL_MODE=require                # disable locally if needed
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=5
DB_POOL_TIMEOUT=10
DB_USE_PGBOUNCER=false             # true if connecting through pgbouncer/pooler

JWT_SECRET=<generate: python -c "import secrets; print(secrets.token_urlsafe(48))">
ANTHROPIC_API_KEY=<provided separately>
LLM_TIMEOUT_SECONDS=45
CORS_ORIGINS=https://<your-frontend-domain>
```

### 1.2 Engine configuration

- `DATABASE_URL` is **required** (no default). Validate it starts with `postgresql+asyncpg://`.
- Create the engine with `pool_pre_ping=True`, `pool_size`, `max_overflow`, `pool_timeout` from settings.
- SSL: pass `connect_args={"ssl": "require"}` when `DB_SSL_MODE=require`.
- If `DB_USE_PGBOUNCER=true`: add `connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0}` (asyncpg + pgbouncer transaction mode).
- `alembic/env.py` must read the URL from settings/env — **not** from `alembic.ini`. Leave `sqlalchemy.url` blank in `alembic.ini`.

### 1.3 Dependencies

- Ensure `asyncpg` is in `requirements.txt`. Remove `aiosqlite` if it is only there for SQLite.

### 1.4 Verify

```bash
alembic upgrade head
python -m app.scripts.seed_agents     # expect 100 agents / 400 sub-agents
uvicorn app.main:app
```

---

## 2. P0 — Blocker (must fix before anything else)

### F1 — Conversation ordering is broken

**Files:** `app/models/message.py:37`, `app/repositories/message_repo.py:54`, `app/services/chat_engine.py:73–117`

**Problem:** user and assistant turns are inserted in one transaction → identical `created_at` (Postgres uses transaction-start time) → history and LLM context come back scrambled.

**Do:**

1. Add a `seq` column to `messages`: `BigInteger`, `Identity(always=False)` (or `autoincrement=True`), `nullable=False`, unique.
2. Alembic migration:
   - Add column as nullable → **backfill** existing rows in order `created_at ASC, (role = 'user') DESC, id` so user comes before assistant on ties → set `NOT NULL` + attach the identity/sequence, set sequence start above `max(seq)`.
   - In the **same migration**, add composite index (covers F7):
     `ix_messages_history (user_id, agent_id, sub_agent_id, seq)`.
3. `get_recent_history`: order by `seq DESC LIMIT n`, then reverse in Python so output is oldest → newest.
4. Ensure the user turn is flushed **before** the assistant turn is added (so `seq` user < `seq` assistant).
5. Make the history window pair-safe: after applying `LIMIT`, if the first message is `assistant`, drop it so LLM context always starts with a `user` message.
6. `GET /agents/{slug}/history`: order by `seq`. Keep `limit` but make it a validated query param (`1–100`, default 50).

**Tests:**
- Send 5 messages → history is strictly `user, assistant, user, assistant, …` in the sent order.
- The context passed to the (mock) provider ends with the current user message and starts with a `user` role.
- Sub-agent threads remain separate.

---

## 3. P1 — High (must fix before pilot)

### F2 — LLM failures unhandled (no timeout, no error mapping)

**Files:** `app/services/chat_engine.py:101`, `app/services/llm/anthropic_provider.py:43–49`

**Do:**
1. Build `AsyncAnthropic(api_key=..., timeout=settings.LLM_TIMEOUT_SECONDS, max_retries=1)`.
2. Wrap `provider.complete()` in `try/except` for `anthropic.APITimeoutError`, `anthropic.APIConnectionError`, `anthropic.RateLimitError`, `anthropic.APIStatusError` → raise a domain error `LLMUnavailableError`.
3. Map `LLMUnavailableError` → **HTTP 503** `{"detail": "The assistant is temporarily unavailable. Please try again."}` with `Retry-After: 30`.
4. **Persistence decision:** persist the user turn, do **not** persist an assistant turn on failure. Document this in the service docstring.
5. Do not hold a DB transaction open during the LLM call: commit/flush the user turn, release, call the LLM, then write the assistant turn.
6. Log the provider error (type + status) without logging the prompt or the API key.

**Tests:** provider stub that raises timeout / 500 / 429 → API returns 503, no 500, no assistant row written.

### F3 — JWT_SECRET silently defaults to a placeholder

**Files:** `app/core/config.py:18, 24`

**Do:**
1. Add a pydantic validator: if `ENVIRONMENT != "development"` and (`JWT_SECRET` equals the dev default **or** `len < 32`) → raise at startup.
2. Actually use `ENVIRONMENT` (it is currently unused). Allowed values: `development | staging | production`.
3. In production also refuse `CORS_ORIGINS="*"`.

**Tests:** settings with `ENVIRONMENT=production` + default secret → raises; strong secret → boots.

### F4 — Auth works on deactivated agents

**Files:** `app/api/routers/auth.py:23–27`, `app/api/routers/agents.py:46–50`, compare `app/services/chat_engine.py:45`

**Do:**
1. Create one shared helper, e.g. `get_active_agent_or_404(slug)`.
2. Use it in `_resolve_main_agent` (signup + login) and in `GET /agents/{slug}`.
3. Inactive agent → **404** (same as unknown slug).

**Tests:** deactivate an agent → detail 404, signup 404, login 404, chat 404, list excludes it.

---

## 4. P2 — Medium (before general availability)

| ID | Fix | Test |
|----|-----|------|
| **F5** | Signup race: catch `sqlalchemy.exc.IntegrityError` on insert → rollback → re-select user → continue existing idempotent path (password ok → token, else 409). | Simulate duplicate insert → 200/409, never 500. |
| **F6** | Add `GET /health/ready` running `SELECT 1` (short timeout) → 200 or 503. Keep `/health` as cheap liveness. Point `render.yaml` `healthCheckPath` to `/health/ready`. | DB down → ready 503, health 200. |
| **F7** | Composite index `(user_id, agent_id, sub_agent_id, seq)` — **done inside the F1 migration**. | `EXPLAIN` on history query uses the index. |
| **F8** | Move `GET /agents` filtering (`q` via `ILIKE`, `industry` case-insensitive, `featured`) and `/industries` `DISTINCT` into SQL. Add optional `limit`/`offset` (default returns current full list so contract is unchanged). | Same results as before for current seed. |
| **F9** | Add `Retry-After` header on 429. Move rate-limit check **after** target resolution so 403/404 requests don't consume budget. (Redis limiter later — see §6.) | 429 has `Retry-After`; 404 calls don't count. |
| **F10** | In `app/core/deps.py:46`, wrap `uuid.UUID(sub)` in `try/except ValueError` → 401. | Signed token with non-UUID `sub` → 401. |
| **F11** | Lock dependencies (pip-tools `requirements.lock` or `uv.lock`). CI + Dockerfile install from the lock. | CI green from lockfile. |

---

## 5. P3 — Low (backlog, do if time allows)

- **F12** — Validate JWT `agent_id` claim against `user.agent_id` (401 on mismatch), or remove the claim.
- **F13** — Protect `/meta/usage` (admin token or disable in production).
- **F14** — Keep signup 409 for now; revisit with email verification.
- **F15** — Request-ID middleware (`X-Request-ID`, generate if absent), include it in every log line and error response; JSON structured logs.
- **F16** — Dockerfile: add non-root `USER`; move `alembic upgrade` + seed to a release/pre-deploy step instead of every boot.
- **F17** — Plan refresh tokens / token versioning before GA (design note only for now).
- **F18** — Remove `python-multipart` unless `/docs` auth form needs it.

---

## 6. Tests & CI

1. Tests run against **PostgreSQL** via `TEST_DATABASE_URL` (never the main DB). Fixture: create schema with `alembic upgrade head`, truncate tables between tests.
2. CI: start a `postgres:16` service container; set `TEST_DATABASE_URL` from CI secrets/service env — **no credentials in the workflow file** beyond the throwaway CI service.
3. New tests required: ordering (F1), provider failure → 503 (F2), secret guard (F3), inactive agent (F4), signup race (F5), readiness (F6), 429 at API level with `Retry-After` (F9), malformed `sub` (F10).
4. All existing 29 tests must still pass.

---

## 7. Definition of Done

- [x] App boots only with a valid PostgreSQL `DATABASE_URL`; no SQLite code path remains
      — Implemented: `4a063af` — `tests/test_config_guard.py`
- [x] No credentials anywhere in the repo (`git grep -i password`, `git grep postgresql://` show only placeholders)
      — Implemented: M0 audit — `git grep` re-run clean this session (see `docs/PROGRESS.md`)
- [x] `alembic upgrade head` runs clean on an empty Postgres DB **and** on a DB with existing messages (backfill)
      — Implemented: `cfff140`, `4cd542e` + this session's `tests/test_migration_backfill.py`
- [x] F1–F4 fixed, each with passing tests
      — Implemented: `cfff140`/`4cd542e` (F1), `cd1a718` (F2), `d3eb52a` (F3), `1daba16` (F4) — see `docs/PROGRESS.md` audit table for per-finding tests
- [x] F5, F6, F9, F10, F11 fixed, each with passing tests
      — Implemented: `5c32e0a` (F5), `28d901e` (F6), `a4c884b` (F9/F10), `cfaa265`+this session (F11) — see `docs/PROGRESS.md` audit table
- [x] All 10 endpoints re-verified live against Postgres (happy + error paths)
      — Implemented: M0 audit, live smoke test against real dev Postgres (port 8123) — see `docs/PROGRESS.md` "Live endpoint smoke test"
- [x] `/health/ready` returns 503 when DB is unreachable
      — Implemented: `28d901e` — `tests/test_signup_race_and_readiness.py`; verified live too
- [x] Production config refuses weak `JWT_SECRET` and wildcard CORS
      — Implemented: `d3eb52a` — `tests/test_config_guard.py`
- [x] Short change log per finding ID in the PR description
      — Implemented as the audit table in `docs/PROGRESS.md` (no PR opened — commits stay local per §2.8; the table is the change log)

---

## 8. Credentials Handover

Credentials (PostgreSQL + Anthropic key) will be shared separately.

1. Put them in a local `.env` (gitignored) or the deploy platform's secret store.
2. Never paste them into code, commits, PR descriptions, logs, or test fixtures.
3. If a credential is ever committed by accident: rotate it immediately, then remove it from history.

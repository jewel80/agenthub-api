# AgentHub API

Backend for **AgentHub** — a multi-tenant *"Play Store for AI agents"*. Visitors
browse ~100 professional AI agents, authenticate **scoped to one agent**, and
chat with it powered by a real LLM.

> **Agents are configuration, not code.** An agent is a DB row (persona +
> system prompt) handed to a generic chat engine. Adding agent #101 is a
> `seed_agents.py` row, never a code change.

**Stack:** Python · FastAPI (async) · SQLAlchemy 2.0 async · Alembic ·
PostgreSQL (Supabase) · Anthropic Claude (behind a swappable interface) ·
argon2 · JWT.

---

## Quick start (local)

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Unix:     source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # set DATABASE_URL (PostgreSQL only) + ANTHROPIC_API_KEY (or LLM_PROVIDER=mock)
alembic upgrade head          # create tables in your PostgreSQL database
python -m app.pipeline.seed_agents        # load 100 agents + 400 sub-agents from CSV
uvicorn app.main:app --reload --port 8000  # http://localhost:8000/docs
```

**PostgreSQL is required** (`postgresql+asyncpg://…`); there is no SQLite
fallback — the app refuses to start without a valid Postgres `DATABASE_URL`.
For a local dev server without SSL, set `DB_SSL_MODE=disable`.

Without an API key, set `LLM_PROVIDER=mock` in `.env` — every flow works with a
deterministic stub reply (great for local dev / CI). With a key, set
`LLM_PROVIDER=anthropic` for real Claude responses.

### Tests

```bash
pytest                        # runs against PostgreSQL via TEST_DATABASE_URL
```

Tests run against a **disposable PostgreSQL database** (set `TEST_DATABASE_URL`
in `.env` — its tables are truncated between tests, so never point it at your
main database) and use the mock provider, so no API key is needed. CI provisions
a `postgres:16` service container automatically.

---

## Environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `ENVIRONMENT` | no | `development` | `development` \| `staging` \| `production` |
| `DATABASE_URL` | yes | — | PostgreSQL only: `postgresql+asyncpg://…` |
| `TEST_DATABASE_URL` | tests | — | Disposable test DB (tables truncated); must never equal `DATABASE_URL` — enforced at test session start ([`app/core/db_safety.py`](app/core/db_safety.py)) |
| `DB_SSL_MODE` | no | `require` | `disable` for local dev without SSL |
| `DB_USE_PGBOUNCER` | no | `false` | `true` behind a transaction-mode pooler |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` / `DB_POOL_TIMEOUT` | no | `10` / `5` / `10` | Connection pool tuning |
| `JWT_SECRET` | yes (non-dev) | dev placeholder | ≥ 32 chars, not the dev default; generate: `python -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `JWT_ALG` | no | `HS256` | |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | no | `10080` (7 days) | |
| `LLM_PROVIDER` | no | `anthropic` | `anthropic` \| `mock` (no API key needed) |
| `ANTHROPIC_API_KEY` | if `anthropic` | — | Direct Anthropic (`x-api-key`) |
| `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` | no | — | Bearer auth + base URL for an Anthropic-compatible gateway (e.g. z.ai), instead of a direct API key |
| `ANTHROPIC_MODEL` | no | `claude-haiku-4-5` | |
| `LLM_TIMEOUT_SECONDS` | no | `45` | Client timeout for LLM calls |
| `LLM_PROMPT_CACHE_ENABLED` | no | `true` | Anthropic prompt caching (system prompt + history prefix) |
| `CORS_ORIGINS` | no | `http://localhost:3000` | Comma-separated frontend URLs |
| `STREAMING_ENABLED` | no | `true` | SSE streaming endpoint toggle |
| `STREAM_MAX_SECONDS` | no | `120` | Max duration of one SSE stream |
| `MAX_CONCURRENT_STREAMS_PER_USER` | no | `2` | Per-user concurrent stream cap; `0` disables |
| `REDIS_URL` | no | — | Optional at runtime: empty/unreachable → in-memory rate limiting, no caching, never crashes. Local dev: `docker compose -f docker-compose.dev.yml up -d` (host port `6380`) |
| `RATE_LIMIT_PER_MIN` | no | `20` | Per-user chat cap; `0` disables |
| `RATE_LIMIT_IP_PER_MIN` | no | `60` | Per-IP cap on unauthenticated routes; `0` disables |
| `LOGIN_RATE_LIMIT_PER_MIN` | no | `10` | Per (email, agent) login attempt cap |
| `LOGIN_MAX_FAILURES` | no | `5` | Failed logins before exponential lockout |
| `DAILY_TOKEN_QUOTA_DEFAULT` | no | `200000` | Per-user daily LLM token quota; `0` disables |
| `GLOBAL_LLM_CONCURRENCY` | no | `10` | Global in-flight LLM call cap; `0` disables |
| `ADMIN_TOKEN` | no | — | Protects `/meta/*` in production (`X-Admin-Token` header); empty in prod → 404 |
| `LOG_FORMAT` | no | `text` | `text` \| `json` (for log shippers) |
| `AGENTS_CSV_PATH` | no | `data/agents_sample.csv` | Source CSV for the pipeline |

---

## API reference

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/agents` | – | Catalog (query `q`, `industry`, `featured`) |
| GET | `/agents/{slug}` | – | One agent + its sub-agents |
| GET | `/industries` | – | Distinct industries (for filters) |
| POST | `/agents/{slug}/signup` | – | Create an account **scoped to that agent** |
| POST | `/agents/{slug}/login` | – | Login scoped to that agent |
| GET | `/me` | bearer | Current user |
| POST | `/agents/{slug}/chat` | bearer | Chat with the agent or a `sub_agent_slug` |
| POST | `/v1/agents/{slug}/chat/stream` | bearer | Same, as Server-Sent Events (`start`/`delta`*/`done`\|`error`); `STREAMING_ENABLED` toggle |
| GET | `/agents/{slug}/history` | bearer | Conversation history (per sub-agent, `limit` 1–100) |
| GET | `/meta/usage` | admin token in prod | Agent usage stats (observability) |
| GET | `/health` | – | Liveness |
| GET | `/health/ready` | – | Readiness (database reachable) |

**How to verify streaming locally:** `curl -N -X POST http://localhost:8000/v1/agents/<slug>/chat/stream -H "Authorization: Bearer <token>" -H "Content-Type: application/json" -d '{"message":"hi"}'` — expect an `event: start` line, one or more `event: delta` lines, then `event: done`.

**How to verify Redis-backed rate limiting/quota locally:** start Redis (`docker compose -f docker-compose.dev.yml up -d`), set `REDIS_URL=redis://localhost:6380/0`, then hit `/agents/{slug}/chat` past `RATE_LIMIT_PER_MIN` or `DAILY_TOKEN_QUOTA_DEFAULT` — expect `429` with `Retry-After` and `X-RateLimit-*` headers. Stop Redis and repeat: the same limits still apply (in-process fallback), confirming a Redis outage never becomes an API outage. See [`docs/runbooks/redis-degradation.md`](docs/runbooks/redis-degradation.md).

---

## Architecture

Layered, dependency-injected, 12-factor:

```
api/routers   → HTTP layer (validation, auth, status codes)
services      → business logic (auth_service, chat_engine, llm/, rate_limiter, observability)
repositories  → DB access (one repo per aggregate)
models        → SQLAlchemy ORM (agents, users, messages)
schemas       → Pydantic DTOs (request/response)
pipeline      → CSV → agent configs (seed_agents.py)
```

### The generic chat engine (`services/chat_engine.py`)

There is **one** code path for every agent:

1. resolve `agent_slug` (+ optional `sub_agent_slug`) → an `agents` row
2. enforce the caller's auth scope (403 on mismatch)
3. merge scoped conversation history
4. call the LLM through the `LLMProvider` interface, using the row's
   `system_prompt` as the persona
5. persist the turn, record usage, return the reply

No `if agent == doctor`. The persona is data. Reviewers can grep the codebase:
zero per-agent branches, zero per-agent files, zero per-agent endpoints.

### Agents are data (`models/agent.py`)

A single `agents` table holds both main agents (`parent_id IS NULL`) and
sub-agents (`parent_id` → parent). Adding an agent is an `INSERT` (done by the
idempotent pipeline). 5 agents are flagged `is_featured` for spotlighting.

### LLM provider abstraction (`services/llm/`)

`LLMProvider` interface → `AnthropicProvider` (primary) / `MockProvider`
(tests). `services/llm/__init__.py` picks one from `LLM_PROVIDER`. Swapping to
OpenAI/Gemini = one new class + one factory line; the engine never changes.

### Content pipeline (`pipeline/seed_agents.py`)

Reads `agents_sample.csv`, **generates** system prompts from templates (main
agent from profession + tasks; each sub-agent from its name + task under the
parent), and upserts by `slug` — so re-running on an updated CSV updates rather
than duplicates. `--llm-polish` optionally refines prompts via the LLM.

---

## Auth isolation — and why this design

**Decision: tenant-scoped shared auth** (not separate auth systems per agent).

- One `users` table with `UNIQUE(email, agent_id)`.
- Signup/login are parameterised by agent: `POST /agents/{slug}/signup`. A
  credential row is bound to exactly one `agent_id`.
- The JWT carries an `agent_id` claim; `require_agent_scope()` validates that
  claim against the requested resource on every protected call (403 on match
  failure). An account created under **Agent A fails under Agent B**.

**Why this over fully-separate auth per agent:**

1. **It serves the "no 100 copies" principle.** Separate auth = 100 duplicated
   tables/endpoints/services — exactly what the assignment says to avoid.
   Tenant-scoping keeps one auth code path while still enforcing isolation.
2. **Same UX as real app stores.** One email can register independently under
   different agents (like different apps), yet a login never crosses agents.
3. **Simpler, less error-prone.** One place to harden (hashing, rate limiting,
   JWT) instead of N copies that drift.

The isolation is *enforced server-side* by the JWT claim check, not just implied
by routing, so a stolen token from Agent A cannot read Agent B's data even if
the client misbehaves.

---

## Deployment

The `Dockerfile` builds a non-root image from the pinned `requirements.lock`
and serves uvicorn only. Migrations run as a **separate release step**, not on
every boot (set `RUN_MIGRATIONS_ON_BOOT=true` for disposable/dev environments):

```bash
docker build -t agenthub-api .
docker run --rm --env-file .env agenthub-api alembic upgrade head   # release step
docker run --env-file .env -p 8000:8000 agenthub-api                # serve
```

- **Render:** `render.yaml` Blueprint is included (health check
  `/health/ready`). Create a Postgres on Supabase (or Neon), set
  `DATABASE_URL` (the `postgresql+asyncpg://` pooler URL),
  `ANTHROPIC_API_KEY`, and `CORS_ORIGINS` (your frontend URL) in the dashboard.
  `JWT_SECRET` auto-generates.
- **Supabase note:** if using the PgBouncer transaction pooler (port 6543),
  set `DB_USE_PGBOUNCER=true` — this disables asyncpg statement caching
  (required in transaction mode) in `core/db.py`.

### Admin path to add a new agent (zero code)

Because agents are rows, the admin "UI" can be a SQL insert or a one-line call:

```bash
# via the pipeline on a new/updated CSV row (idempotent):
python -m app.pipeline.seed_agents

# or directly:
psql "$DATABASE_URL" -c "INSERT INTO agents (slug, industry, profession, \
  tagline, description, system_prompt) VALUES ('notary', 'Legal Services', \
  'Notary', 'Your AI notary', 'desc', 'You are a senior Notary…');"
```

Either way, the new agent immediately appears in the catalog and chats through
the *same* engine — no deploy, no code change.

---

## What I'd do differently with more time

- **Email verification + refresh tokens** (current access JWT is long-lived for demo simplicity;
  see the design note for the planned refresh-token/token-versioning scheme).
- **LLM-polished prompts by default** + per-agent tunable model/temperature config columns.
- **Full-text search** on the catalog (`pg_trgm`; currently `ILIKE` in SQL, which is fine at this scale).
- **OpenAPI client generation** to share types with the frontend end-to-end.

Done since the initial pass: **streaming chat** (SSE, `POST /v1/agents/{slug}/chat/stream`)
and a **Redis-backed** rate limiter + daily token quota + login lockout, both with an
in-process fallback so a missing/unreachable Redis never takes the API down.

## Known limitations

- Rate limiter, quota, and login-lockout state is **Redis-backed when `REDIS_URL` is set
  and reachable** (shared across instances); otherwise it degrades to **in-process**
  (single-instance only) state automatically.
- No refresh-token rotation; access token lifetime is 7 days for convenience.
- Sub-agent slugs are globally unique (prefixed with the parent slug) so the
  unique constraint holds; resolution is parent-scoped for security.
- Supabase transaction-pooler prepared-statement caveat is handled, but if you
  switch poolers, double-check `core/db.py` connect args.

## AI-assisted development disclosure

This project was built with **Claude Code** (Anthropic) as a pairing tool —
architecture, implementation, tests, and docs were produced with AI assistance
and reviewed/iterated by the author. Per the assignment rules, this use of an
AI coding agent is disclosed here.

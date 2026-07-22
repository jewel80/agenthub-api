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

cp .env.example .env          # then edit: set ANTHROPIC_API_KEY (or LLM_PROVIDER=mock)
alembic upgrade head          # create tables (SQLite by default — zero setup)
python -m app.pipeline.seed_agents        # load 100 agents + 400 sub-agents from CSV
uvicorn app.main:app --reload --port 8000  # http://localhost:8000/docs
```

Without an API key, set `LLM_PROVIDER=mock` in `.env` — every flow works with a
deterministic stub reply (great for local dev / CI). With a key, set
`LLM_PROVIDER=anthropic` for real Claude responses.

### Tests

```bash
pytest                        # 28 tests, no external services required
```

Tests use an in-memory SQLite DB + the mock provider, so they run anywhere with
no secrets.

---

## Environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `DATABASE_URL` | yes (prod) | `sqlite+aiosqlite:///./agenthub.db` | Supabase: `postgresql+asyncpg://…` |
| `JWT_SECRET` | yes (prod) | dev placeholder | Generate: `python -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `LLM_PROVIDER` | no | `anthropic` | `anthropic` \| `mock` |
| `ANTHROPIC_API_KEY` | if `anthropic` | — | |
| `ANTHROPIC_MODEL` | no | `claude-haiku-4-5` | |
| `CORS_ORIGINS` | no | `http://localhost:3000` | Comma-separated frontend URLs |
| `RATE_LIMIT_PER_MIN` | no | `20` | Per-user chat cap; `0` disables |
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
| GET | `/agents/{slug}/history` | bearer | Conversation history (per sub-agent) |
| GET | `/meta/usage` | – | Agent usage stats (observability) |
| GET | `/health` | – | Liveness |

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

The included `Dockerfile` runs migrations + seeds on boot, then serves uvicorn.
It works on Render, Railway, or Fly.io.

- **Render:** `render.yaml` Blueprint is included. Create a Postgres on Supabase
  (or Neon), set `DATABASE_URL` (the `postgresql+asyncpg://` pooler URL),
  `ANTHROPIC_API_KEY`, and `CORS_ORIGINS` (your frontend URL) in the dashboard.
  `JWT_SECRET` auto-generates.
- **Supabase note:** if using the PgBouncer transaction pooler (port 6543),
  asyncpg needs `statement_cache_size=0` — already applied automatically in
  `core/db.py` for any `postgres` URL.

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

- **Streaming chat** (SSE) so tokens render live instead of waiting for the full reply.
- **Redis-backed** rate limiter + usage counters (current ones are per-process)
  for multi-instance deploys.
- **Email verification + refresh tokens** (current access JWT is long-lived for demo simplicity).
- **LLM-polished prompts by default** + per-agent tunable model/temperature config columns.
- **Pagination + full-text search** on the catalog (currently loads all 100 client-side, which is fine at this scale).
- **OpenAPI client generation** to share types with the frontend end-to-end.

## Known limitations

- Rate limiter and usage tracker are **in-process** (single-instance only).
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

# AgentHub API — Production-Grade Improvement Roadmap

> **Audience:** the developer / coding agent implementing the upgrades
> **Prerequisite:** finish everything in `AGENTHUB_FIX_INSTRUCTIONS.md` (P0 + P1 at least) **before** starting here.
> **Goal:** take AgentHub from "working MVP" to a **global-standard, production-grade AI chat API**: fast (streaming), cheaper (caching), scalable (multiple instances), observable, secure and compliant.

---

## 0. Ground Rules (same as the fix doc)

1. **Database = PostgreSQL only** (`postgresql+asyncpg://`). No SQLite anywhere.
2. **No credentials in code.** All secrets (Postgres, Redis, Anthropic key, Sentry DSN, etc.) come from environment variables or the platform's secret store. Credentials will be provided separately. `.env.example` has placeholders only.
3. Every schema change → Alembic migration. Every feature → tests.
4. New features go behind a **feature flag / env toggle** so they can be switched off without a redeploy.
5. Keep the existing non-streaming `POST /chat` working. Streaming is **added**, not a replacement.

---

## 1. Target Architecture (high level)

```
Client (web / mobile)
   │  HTTPS, JWT
   ▼
CDN / Edge (TLS, WAF, gzip, static cache)
   ▼
Load Balancer
   ▼
FastAPI instances (stateless, N replicas, non-root container)
   │        │              │
   │        │              └──► Anthropic API (streaming, prompt caching, timeout, circuit breaker)
   │        └──► Redis  (cache, rate limit, usage counters, locks, token blocklist)
   └──► PostgreSQL (primary + read replica, PgBouncer, PITR backups)
            ▲
Background worker (arq / Celery / RQ)  ──► summaries, cleanup, analytics, emails
```

**Principle:** the API process holds **no state**. Everything shared (limits, cache, sessions, counters) lives in Redis or Postgres, so you can run 1 or 20 instances.

---

## 2. Phase Plan

| Phase | Theme | Items | When |
|------|-------|-------|------|
| **Phase 1** | Core UX + cost | Chat streaming (§3), LLM prompt caching (§4.1), Redis-backed rate limit (§5) | Right after P0/P1 fixes |
| **Phase 2** | Scale + reliability | App caching (§4.2–4.4), context management (§6), resilience (§7), API standards (§8) | Before GA |
| **Phase 3** | Operate at scale | Observability (§9), security (§10), data/DB (§11), CI/CD + infra (§12) | GA |
| **Phase 4** | Mature product | Privacy/compliance (§13), AI quality + safety (§14), cost controls (§15), admin (§16) | Post-GA |

---

## 3. Chat Streaming (Phase 1)

**Why:** users see the first words in < 1 s instead of waiting 5–20 s for the full reply. This is the standard for every major AI chat app.

### 3.1 Endpoint

- New: `POST /v1/agents/{slug}/chat/stream` → `Content-Type: text/event-stream` (Server-Sent Events).
- Same auth, same request body, same tenant/sub-agent checks, same rate limit as `/chat`.
- Use FastAPI `StreamingResponse` (or `sse-starlette`).
- Use `client.messages.stream(...)` from the Anthropic SDK.

### 3.2 Event format (fixed contract)

```text
event: start
data: {"message_id": "<uuid>", "conversation_id": "<uuid>"}

event: delta
data: {"text": "Hello"}

event: delta
data: {"text": ", how can I help?"}

event: done
data: {"message_id": "<uuid>", "usage": {"input_tokens": 812, "output_tokens": 64, "cache_read_tokens": 700}, "stop_reason": "end_turn"}

event: error
data: {"code": "llm_unavailable", "message": "The assistant is temporarily unavailable."}
```

- Send a heartbeat comment (`: ping`) every ~15 s so proxies don't close the connection.
- Set headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no` (stops nginx buffering).

### 3.3 Persistence rules

1. Persist the **user** turn before streaming starts (commit, then release the DB session — do **not** hold a DB connection open during the stream).
2. Accumulate the assistant text in memory while streaming.
3. On `done` → open a new short session and save the assistant turn with token usage.
4. On **client disconnect** → stop the upstream stream, save the partial reply with `status = "interrupted"`.
5. On **LLM error mid-stream** → send an `error` event, save the partial reply with `status = "failed"`.
6. Add a `status` column to `messages` (`complete | interrupted | failed`) via migration.

### 3.4 Limits

- Max stream duration (e.g. 120 s) and max output tokens per agent (from agent config).
- Cap concurrent streams per user (e.g. 2) using a Redis counter.

### 3.5 Tests

- Mock provider that yields chunks → the client receives `start`, several `delta`, and `done` in that order.
- Disconnect mid-stream → partial message saved as `interrupted`.
- Provider raises mid-stream → `error` event, no 500.

> **Optional later:** WebSocket endpoint for two-way features (typing indicators, cancel button). SSE is enough for v1.

---

## 4. Caching Strategy (Phases 1–2)

Four layers, from biggest win to smallest.

### 4.1 LLM prompt caching — Anthropic (biggest cost + latency win)

- Every agent has a long, stable `system_prompt`. Mark it cacheable:
  `system=[{"type": "text", "text": agent.system_prompt, "cache_control": {"type": "ephemeral"}}]`
- Also put a cache breakpoint on the **conversation history prefix** (all turns except the newest), so follow-up messages reuse the cached prefix.
- Order the prompt as **stable → variable**: tools → system prompt → older history → newest user message.
- Log `cache_creation_input_tokens` and `cache_read_input_tokens` from the response; show the hit rate on the dashboard (§9).
- Note: caching only activates above a minimum prompt length (depends on model). Check the current Anthropic docs for the exact minimums.

### 4.2 Application cache — Redis

| What | Key | TTL | Invalidate when |
|------|-----|-----|-----------------|
| Agent catalog (`GET /agents` + filters) | `catalog:v{ver}:{hash(query)}` | 5–10 min | Agent created / updated / deactivated → bump `ver` |
| Agent detail (`GET /agents/{slug}`) | `agent:{slug}:v{ver}` | 10 min | Same as above |
| Industries list | `industries:v{ver}` | 30 min | Same as above |
| Agent config for chat (system prompt, model, limits) | `agentcfg:{agent_id}` | 5 min | Agent update |
| User lookup for JWT auth (`/me`, deps) | `user:{id}` | 60 s | User update / deactivate |

Rules:
- **Cache-aside pattern:** read cache → miss → read DB → write cache.
- **Never cache** chat replies or anything per-user sensitive except the short user lookup.
- Protect against stampede: use a short Redis lock (`SET NX`) when rebuilding a hot key.
- If Redis is down, **fall back to DB**. A cache outage must never be an API outage.
- Wrap it in one `CacheService` so the implementation can be swapped.

### 4.3 HTTP caching (public catalog)

- `GET /agents`, `/agents/{slug}`, `/industries`: add `ETag` + `Cache-Control: public, max-age=60, stale-while-revalidate=300`.
- Support `If-None-Match` → `304 Not Modified`.
- Authenticated endpoints: `Cache-Control: private, no-store`.

### 4.4 CDN / edge

- Put the public catalog endpoints behind a CDN (Cloudflare / CloudFront / Render's edge) with respect for `Cache-Control`.

---

## 5. Distributed Rate Limiting & Quotas (Phase 1)

Replaces the in-memory limiter (review finding F9), which is required before running more than one instance.

- Redis-backed **sliding window** or **token bucket** (e.g. `limits` library, or a Lua script).
- Limits per layer:
  - per IP (anti-abuse, unauthenticated routes): e.g. 60 req/min
  - per user (chat): e.g. 20 msgs/min
  - per user **daily token quota** (cost control): e.g. 200k tokens/day, configurable by plan
  - global LLM concurrency cap (protects your Anthropic rate limit)
- Always return `429` + `Retry-After` + `X-RateLimit-Limit / -Remaining / -Reset` headers.
- Login/signup: stricter limit + exponential lockout after repeated failures (brute-force protection).
- Move usage counters (`/meta/usage`) to Redis/Postgres so they survive restarts.

---

## 6. Conversation & Context Management (Phase 2)

- **Conversations as a first-class entity:** add a `conversations` table (id, user_id, agent_id, sub_agent_id, title, created_at, updated_at, archived). Messages belong to a conversation. Users can start new chats, list, rename, and delete them.
  - Endpoints: `GET/POST /v1/agents/{slug}/conversations`, `GET/PATCH/DELETE /v1/conversations/{id}`, `GET /v1/conversations/{id}/messages?cursor=`.
- **Token-budget context window:** build the context by **token count**, not a fixed "last 20 messages". Keep the system prompt + newest turns that fit the budget.
- **Rolling summarization:** when history exceeds the budget, a background job summarizes older turns into a `conversation_summary` stored in Postgres and injected into the context.
- **Auto title:** after the first exchange, a background job generates a short conversation title (cheap/fast model).
- **Regenerate / edit last message:** `POST /v1/conversations/{id}/regenerate`.
- **Feedback:** `POST /v1/messages/{id}/feedback` (👍/👎 + optional comment) → table `message_feedback`. This is used for quality monitoring (§14).

---

## 7. Resilience & Reliability (Phase 2)

- **Timeouts everywhere:** LLM (from fix doc), DB statement timeout (`statement_timeout` e.g. 5 s), Redis (≤ 200 ms).
- **Retries with exponential backoff + jitter** only for safe, idempotent calls (LLM 429/529/5xx, max 2).
- **Circuit breaker** around the LLM provider: after N failures in a window, fail fast with 503 for a cool-down period instead of piling up requests.
- **Idempotency keys:** accept an `Idempotency-Key` header on `POST /chat` and `/signup`; store the result in Redis for 24 h so client retries don't create duplicate messages or charges.
- **Graceful shutdown:** on SIGTERM, stop accepting new requests, let in-flight streams finish (up to ~30 s), close DB/Redis pools.
- **Health checks:** `/health` (liveness), `/health/ready` (DB + Redis). The LLM is **not** part of readiness, so an Anthropic outage doesn't take the pods out of rotation.
- **Provider abstraction:** keep the `LLMProvider` interface so a fallback model or provider can be configured per agent (e.g. fall back to a smaller model when the primary is overloaded).
- **Background jobs** (arq or Celery with Redis): summaries, titles, analytics rollups, data exports, cleanup. Never do slow work inside the request.

---

## 8. API Design Standards (Phase 2)

- **Versioning:** prefix everything with `/v1`. Keep old paths as aliases temporarily with a `Deprecation` header.
- **Standard error format:** RFC 9457 Problem Details:
  ```json
  {"type": "https://api.agenthub.example/errors/rate-limited", "title": "Too Many Requests",
   "status": 429, "detail": "Chat limit reached. Try again in 20 seconds.",
   "code": "rate_limited", "request_id": "req_..."}
  ```
  Stable machine-readable `code` values, documented in one place.
- **Cursor pagination** for history, conversations, and the catalog (`?limit=&cursor=` → `next_cursor`). Cursor = opaque base64 of `seq` / id.
- **OpenAPI:** keep it accurate; add examples, error responses, and auth to every route. Publish the docs; disable `/docs` in production or protect it.
- **Consistent naming:** snake_case JSON, ISO-8601 UTC timestamps, UUIDs for public ids.
- **CORS:** explicit allow-list from env; never `*` in production.
- **Compression:** gzip/brotli for JSON responses (not for SSE).
- **Request size limits** at the edge and app level.

---

## 9. Observability (Phase 3)

The goal: know about a problem **before** users report it.

- **Structured JSON logs** with `request_id`, `user_id` (hashed), `agent_id`, route, status, latency. Never log passwords, tokens, API keys, or full message content in production.
- **Request ID:** accept/generate `X-Request-ID`, return it in every response and error body.
- **Tracing:** OpenTelemetry (FastAPI + SQLAlchemy + httpx instrumentation) → any OTLP backend (Grafana Tempo, Honeycomb, Datadog).
- **Metrics:** Prometheus `/metrics` (protected), minimum:
  - request rate / error rate / latency p50-p95-p99 per route
  - LLM: latency, **time-to-first-token**, tokens in/out, cache hit rate, errors by type, cost per agent
  - DB pool usage, slow queries; Redis latency; active streams
- **Error tracking:** Sentry (DSN from env) with PII scrubbing.
- **Dashboards + alerts** (Grafana or vendor): 5xx rate > 1 %, p95 latency, LLM error spike, DB pool > 80 %, daily cost over budget.
- **SLOs (suggested starting point):** 99.9 % availability for non-chat endpoints; chat time-to-first-token p95 < 2 s; error rate < 0.5 %.

---

## 10. Security Hardening (Phase 3)

Follow **OWASP API Security Top 10**.

- **Auth upgrade:** short-lived access token (15 min) + rotating refresh token (stored hashed in Postgres, httpOnly cookie for web). Endpoints: `/auth/refresh`, `/auth/logout`, `/auth/logout-all`. Token version column on users for instant revocation.
- **Email verification + password reset** (signed, single-use, expiring tokens; send via a background job).
- **Optional:** OAuth social login (Google/Apple) and MFA for admin accounts.
- **Security headers:** HSTS, `X-Content-Type-Options`, `Referrer-Policy`, `Content-Security-Policy` for any HTML (docs).
- **Secrets:** move to a secret manager (Render secrets / AWS Secrets Manager / Doppler / Vault). Rotate the JWT secret and API keys on a schedule; support 2 active JWT keys (`kid`) for zero-downtime rotation.
- **Dependency + code scanning in CI:** `pip-audit`, Bandit, Trivy (container image), Dependabot/Renovate, secret scanning (gitleaks).
- **Admin/internal endpoints** (`/meta/*`, `/metrics`, `/docs`) protected by role or network.
- **Pen test** before GA.

---

## 11. Database & Data (Phase 3)

- **PgBouncer** (transaction mode) in front of Postgres once there are multiple instances; set `statement_cache_size=0` for asyncpg.
- **Read replica** for heavy reads (catalog, history listing, analytics); writes stay on the primary.
- **Messages table growth:** plan monthly **partitioning** by `created_at` once it passes ~tens of millions of rows; archive old partitions.
- **Backups:** automated daily backups + **point-in-time recovery**; test a restore every quarter. Define RPO (e.g. ≤ 5 min) and RTO (e.g. ≤ 1 h).
- **Migrations safety:** zero-downtime pattern (expand → migrate data → contract), `lock_timeout` on migrations, never rewrite big tables in one step.
- **Soft delete + audit:** `deleted_at` on users/conversations; `audit_log` table for admin actions and agent config changes.
- **Useful indexes:** `pg_trgm` GIN index for catalog search (`q`), plus the composite history index from the fix doc.
- **Token usage table:** `llm_usage (user_id, agent_id, model, input_tokens, output_tokens, cache_read_tokens, cost_usd, created_at)`, the source of truth for quotas and billing.

---

## 12. CI/CD, Infrastructure & Deployment (Phase 3)

- **Pipeline:** lint (ruff) → type check (mypy) → unit tests → integration tests on a Postgres + Redis service container → security scans → build image → deploy to **staging** → smoke tests → manual approve → **production**.
- **Environments:** `development`, `staging` (production-like, separate DB and keys), `production`.
- **Container:** multi-stage build, slim base, non-root user, pinned lockfile, `HEALTHCHECK`.
- **Migrations** run as a separate release/pre-deploy step, not on every container boot.
- **Deploy strategy:** rolling or blue-green, with automatic rollback on failed health checks.
- **Autoscaling** on CPU + active-connection count; minimum 2 instances in production for high availability.
- **Infrastructure as Code** (Terraform or the Render blueprint) with no secrets inside.
- **Load testing** (k6 or Locust) before GA: catalog RPS, concurrent streams (e.g. 500), signup bursts. Record the baseline and re-run on major changes.

---

## 13. Privacy & Compliance (Phase 4)

- **Data export:** `GET /v1/me/export` → background job builds a JSON/ZIP of the user's conversations.
- **Account deletion:** `DELETE /v1/me` → hard-delete or anonymize within a defined SLA (GDPR "right to erasure").
- **Retention policy:** configurable per environment (e.g. delete messages older than N months); run it as a scheduled job.
- **Encryption:** TLS in transit; encryption at rest on Postgres and backups. Consider column-level encryption for sensitive fields.
- **PII minimization:** do not send user emails or ids to the LLM; scrub PII in logs.
- **Legal pages + consent:** Terms, Privacy Policy, and a clear AI disclaimer (especially for legal, medical, and financial agents).
- **Data residency:** document where the data lives (DB region, LLM provider), which matters for EU and enterprise customers.

---

## 14. AI Quality & Safety (Phase 4)

- **Input moderation / abuse checks** before the LLM call (a cheap classifier or the provider's safety features); block or flag.
- **Prompt-injection defence:** never put secrets in system prompts; treat user content as untrusted; keep system prompts private (already done: not exposed in schemas).
- **Output guardrails** for high-risk industries (legal, medical, financial): a mandatory disclaimer, and a refusal policy defined in the agent config.
- **Agent config versioning:** store `system_prompt` versions (`agent_versions` table) so you can roll back and A/B test prompts.
- **Evaluation suite:** a set of golden conversations per top agent, run automatically when a prompt or model changes; track quality scores over time.
- **Feedback loop:** 👍/👎 data (§6) is reviewed weekly; low-scoring agents get prompt fixes.

---

## 15. Cost Controls (Phase 4)

- Per-user and per-plan **token quotas** (§5), enforced before the LLM call.
- **Model routing:** a cheap/fast model for titles, summaries, and classification; the main model for chat.
- Set `max_tokens` per agent; trim context by token budget (§6).
- Track cost per agent / per user / per day in `llm_usage`; alert on anomalies (e.g. one user burning 10× the normal amount).
- Prompt caching (§4.1) hit-rate target: > 60 % of input tokens on multi-turn chats.

---

## 16. Admin & Product Operations (Phase 4)

- **Admin API / panel** (role-based): manage agents and sub-agents (create, edit, activate/deactivate, prompt versions), view users, usage, and feedback. Every change goes to `audit_log` and invalidates the cache (§4.2).
- **Feature flags** (env-based at first; Unleash / GrowthBook later): streaming, new models, summarization.
- **Status page** + incident runbooks (LLM outage, DB failover, Redis down, key leak).
- **Plans/billing hooks** (if monetizing): plan → quota mapping; Stripe webhooks handled idempotently.

---

## 17. New Environment Variables (placeholders only)

```dotenv
# Redis
REDIS_URL=redis://<USER>:<PASSWORD>@<HOST>:<PORT>/0

# Streaming
STREAMING_ENABLED=true
STREAM_MAX_SECONDS=120
MAX_CONCURRENT_STREAMS_PER_USER=2

# Caching
CACHE_ENABLED=true
CATALOG_CACHE_TTL_SECONDS=300
LLM_PROMPT_CACHE_ENABLED=true

# Limits & quotas
RATE_LIMIT_CHAT_PER_MIN=20
RATE_LIMIT_IP_PER_MIN=60
DAILY_TOKEN_QUOTA_DEFAULT=200000

# Auth
ACCESS_TOKEN_TTL_MIN=15
REFRESH_TOKEN_TTL_DAYS=30

# Observability
SENTRY_DSN=<provided separately>
OTEL_EXPORTER_OTLP_ENDPOINT=<provided separately>
LOG_FORMAT=json
```

All values are read through `app/core/config.py`. **Real values are provided separately and never committed.**

---

## 18. Definition of Done — Production Grade

- [ ] Streaming endpoint live; time-to-first-token p95 < 2 s; disconnects and errors handled and persisted correctly
- [ ] Prompt caching on; cache hit rate visible on the dashboard
- [ ] Redis cache for catalog/agent config with invalidation; the API still works when Redis is down
- [ ] Redis rate limiting + daily token quota; `429` with standard headers
- [ ] 2+ stateless instances running behind a load balancer with no behavior difference
- [ ] `/v1` versioning, RFC 9457 errors, cursor pagination, idempotency keys
- [ ] Structured logs + request IDs + tracing + metrics + Sentry + alerts configured
- [ ] Refresh tokens, logout/revocation, email verification, password reset
- [ ] PgBouncer, backups with a tested restore, zero-downtime migrations
- [ ] CI with tests on Postgres + Redis, security scans, staging → production pipeline
- [ ] Load test passed at the target concurrency; results recorded
- [ ] Data export/deletion, retention job, AI disclaimers for high-risk agents
- [ ] No credentials anywhere in the repo

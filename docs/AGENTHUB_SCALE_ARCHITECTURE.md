# AgentHub API: Scale & Architecture Improvement Instructions

> **Audience:** the developer / coding agent implementing the work
> **Order of work:**
> 1. `AGENTHUB_FIX_INSTRUCTIONS.md` (bug fixes, P0–P3)
> 2. `AGENTHUB_IMPROVEMENT_ROADMAP.md` (streaming, prompt caching, observability, security)
> 3. **This file** (read/write separation, multi-layer caching, event-driven design, scale features)
>
> **Goal:** make AgentHub scale horizontally and survive component failures (DB replica, Redis, LLM) without an outage, following common industry practice.

---

## 0. Instructions for the Implementer

You are working on the AgentHub FastAPI codebase (SQLAlchemy async, Alembic, PostgreSQL). Implement the sections below **in the order of §10**. Follow these rules without exception:

1. **PostgreSQL only** (`postgresql+asyncpg://`). No SQLite in code, tests, or CI.
2. **No credentials in code.** Every URL, password, and key comes from environment variables or the platform secret store. Credentials will be provided separately. `.env.example` contains placeholders only. Do not print secrets in logs, errors, tests, or PR descriptions.
3. **Safe defaults.** Every new feature has an env toggle. When a toggle is off, or an optional dependency (replica, Redis, queue) is missing or down, the app must **fall back to the current behavior**, never crash.
4. **Schema changes** → Alembic migrations only, using the zero-downtime pattern (expand → backfill → contract).
5. **Tests** for every section, run against PostgreSQL + Redis service containers.
6. **No public API contract changes** unless a section says so.
7. One PR per section, titled `feat(scale-§N): ...`, with a short "how to verify" note.

---

## 1. Database Read/Write Separation

### 1.1 Configuration

```dotenv
DATABASE_URL=postgresql+asyncpg://<USER>:<PASSWORD>@<PRIMARY_HOST>:<PORT>/<DB>        # writer (required)
DATABASE_READ_URL=postgresql+asyncpg://<USER>:<PASSWORD>@<REPLICA_HOST>:<PORT>/<DB>   # reader (optional)
DB_READ_REPLICA_ENABLED=false
DB_REPLICA_MAX_LAG_SECONDS=2
READ_YOUR_WRITES_WINDOW_SECONDS=5
```

- If `DATABASE_READ_URL` is empty or `DB_READ_REPLICA_ENABLED=false`, the reader **uses the primary**. The code path stays the same; only the target changes.

### 1.2 Implementation

1. In `app/db/session.py` create **two engines** and **two session factories**: `WriterSession` and `ReaderSession`. Each has its own pool settings and `pool_pre_ping=True`.
2. Provide two FastAPI dependencies: `get_write_db()` and `get_read_db()`.
3. Reader sessions are **read-only**: run `SET TRANSACTION READ ONLY` (or `default_transaction_read_only=on` via connect args), so a misrouted write fails loudly in tests.
4. Routing table (enforce in the repositories/routers):

| Operation | Session |
|---|---|
| signup, login, chat (all writes), conversation create/update/delete, feedback | **Writer** |
| history / messages immediately after a write by the same user | **Writer** (read-your-writes) |
| `GET /agents`, `/agents/{slug}`, `/industries` | Reader |
| conversation list, older message pages | Reader (unless read-your-writes is active) |
| admin reports, exports, analytics jobs | Reader |
| anything inside a transaction that also writes | **Writer** |

5. **Read-your-writes:** after any write by a user, set the Redis key `agenthub:{env}:ryw:{user_id}` with TTL `READ_YOUR_WRITES_WINDOW_SECONDS`. `get_read_db()` checks the key and returns a writer session if it is present. If Redis is down → use the writer (safe).
6. **Replica lag guard:** a background task checks lag every 5 s:
   `SELECT EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()))` on the replica.
   If lag > `DB_REPLICA_MAX_LAG_SECONDS`, or the replica is unreachable → mark the replica unhealthy and route **all reads to the primary** until it recovers. Expose the state as a metric (`db_replica_healthy`, `db_replica_lag_seconds`).
7. Readiness (`/health/ready`) requires the **primary** only. Replica health is reported but is not fatal.
8. Connection pooling: document PgBouncer (transaction mode) in front of both primary and replica. With PgBouncer, set `statement_cache_size=0` and `prepared_statement_cache_size=0` in asyncpg connect args (controlled by `DB_USE_PGBOUNCER`).
9. Failover: use a managed Postgres with automatic failover (RDS/Aurora, Cloud SQL, Neon, Supabase, or Patroni if self-hosted). The app always connects through a **DNS endpoint**, never a hard-coded IP.

### 1.3 Tests

- A reader session rejects an INSERT.
- A write followed by an immediate history read returns the new message (read-your-writes).
- A simulated unhealthy replica routes reads to the primary.
- With replica disabled, every endpoint behaves exactly as before.

---

## 2. Multi-Layer Caching

### 2.1 Layers

```
Client/CDN (HTTP cache) → L1 in-process (TTL LRU) → L2 Redis → PostgreSQL
```

### 2.2 Configuration

```dotenv
REDIS_URL=redis://<USER>:<PASSWORD>@<HOST>:<PORT>/0
CACHE_ENABLED=true
CACHE_L1_ENABLED=true
CACHE_L1_MAX_ITEMS=5000
CACHE_DEFAULT_TTL_SECONDS=300
CACHE_TTL_JITTER_PCT=10
CACHE_NEGATIVE_TTL_SECONDS=30
```

### 2.3 Implementation

1. Create one `CacheService` (`app/services/cache.py`) with `get`, `set`, `get_or_load(key, loader, ttl)`, `invalidate(namespace)`. No other module talks to Redis for caching directly.
2. **Key convention:** `agenthub:{env}:{namespace}:v{version}:{id}`, for example `agenthub:prod:agent:v7:corporate-lawyer`.
3. **Versioned namespaces:** keep the version counter in Redis (`agenthub:{env}:ver:{namespace}`). Invalidation = `INCR` the version, so old keys become unreachable and expire through their TTL. No `KEYS`/`SCAN` deletes.
4. **Cache-aside** via `get_or_load`: L1 → L2 → loader (DB) → write L2 + L1.
5. **TTL jitter:** actual TTL = `ttl ± CACHE_TTL_JITTER_PCT%` to avoid synchronized expiry.
6. **Stampede protection:** on a miss, take a short Redis lock (`SET key:lock NX PX 5000`). The lock holder loads; others wait briefly (≤ 200 ms, polling) and then read the cache, or load from the DB if it's still empty.
7. **Stale-while-revalidate:** store `{value, fresh_until}` with a Redis TTL longer than `fresh_until`. Once past `fresh_until`, return the stale value and refresh in a background task (holding the lock).
8. **Negative caching:** cache "not found" results (unknown agent slug) with `CACHE_NEGATIVE_TTL_SECONDS`.
9. **L1 in-process cache** (`cachetools.TTLCache`): short TTL (≤ 30 s), only for small hot objects (agent config, industries).
10. **Cross-instance L1 invalidation:** on any version bump, publish on Redis channel `agenthub:{env}:cache-invalidate`. Every instance subscribes at startup and clears the matching L1 namespace.
11. **Serialization:** `orjson` (or `msgpack`) with a schema version field. Cache Pydantic DTOs, never ORM objects.
12. **Degradation:** any Redis error → log a warning, increment metric `cache_errors_total`, and go straight to the DB. Use a Redis client timeout ≤ 200 ms and a circuit breaker so a slow Redis doesn't slow the API.

### 2.4 What to cache

| Data | Namespace | TTL | Invalidated by |
|---|---|---|---|
| Catalog list per query | `catalog` | 300 s | Agent/sub-agent create/update/(de)activate |
| Agent detail | `agent` | 600 s | Same |
| Industries | `industries` | 1800 s | Same |
| Agent chat config (system prompt, model, limits) | `agentcfg` | 300 s (L1 30 s) | Agent update / prompt version change |
| User auth lookup | `user` | 60 s | User update / deactivate / password change / logout-all |

**Never cache:** chat replies, message history, passwords, tokens, or any response to an authenticated request except the short user lookup.

### 2.5 HTTP layer

- Public catalog endpoints: strong `ETag` (hash of the cached payload + version) and `Cache-Control: public, max-age=60, stale-while-revalidate=300`; answer `If-None-Match` with `304`.
- Authenticated endpoints: `Cache-Control: private, no-store`.

### 2.6 Redis production requirements (document in README/infra)

- Managed Redis with a replica + automatic failover (ElastiCache, Upstash, Redis Cloud); TLS on.
- `maxmemory` set, `maxmemory-policy allkeys-lru`.
- Separate logical usage: cache vs. rate-limit/locks (separate DB index or separate instance), so cache eviction never deletes rate-limit state.

### 2.7 Optional: semantic LLM cache

- Off by default (`SEMANTIC_CACHE_ENABLED=false`), enabled **per agent** via agent config (only for FAQ-style agents).
- Never for legal, medical, financial, or personalized agents. Never across users unless the agent is explicitly marked `shared_answers=true`.

### 2.8 Tests

- Hit/miss/invalidate cycle; version bump makes old data invisible.
- Stampede: 50 concurrent misses → 1 DB load.
- Redis down → endpoints still return correct data.
- L1 invalidation across two app instances (two app clients sharing one Redis).

---

## 3. Event-Driven Side Effects: Transactional Outbox

**Why:** usage accounting, analytics, titles, summaries, webhooks, and emails must not slow down or break the request, and must not be lost.

1. Migration: table `outbox_events (id uuid, event_type text, payload jsonb, created_at, published_at null, attempts int, last_error text)` + index on `(published_at) WHERE published_at IS NULL`.
2. Write the event **in the same DB transaction** as the business change (e.g. assistant message saved → `chat.completed` event).
3. A relay worker polls unpublished rows (`FOR UPDATE SKIP LOCKED`, batch 100) → publishes to **Redis Streams** (start here; RabbitMQ/Kafka later behind the same interface) → sets `published_at`.
4. Consumers (arq/Celery workers) handle events **idempotently** (dedupe on event id). Retry with exponential backoff; after N failures, move the event to a dead-letter stream and alert.
5. Initial events: `user.signed_up`, `chat.completed` (tokens, cost, agent), `message.feedback`, `agent.updated` (→ cache invalidation).
6. Cleanup job deletes published events older than 7 days.

**Tests:** event is written iff the transaction commits; relay publishes exactly once per event; consumer is idempotent on a duplicate delivery.

---

## 4. Analytics Separation (OLAP ≠ OLTP)

- No reporting or heavy aggregate queries on the primary. Admin dashboards read from the **replica** (short term) or a **warehouse** (long term).
- `chat.completed` and other events feed a warehouse (ClickHouse, BigQuery, or Postgres analytics DB) via a consumer. Only IDs and metrics; **no message content** unless the privacy policy allows it.
- Daily rollup tables: usage per agent / per user / per day, cost, error rate.

---

## 5. Search & Knowledge (RAG with pgvector)

1. **Catalog search:** `pg_trgm` GIN index on agent name/description; `ILIKE`/similarity query in SQL (replaces Python filtering).
2. **Agent knowledge base (optional feature flag `RAG_ENABLED`):**
   - Enable the `pgvector` extension via migration.
   - Tables: `kb_documents (id, agent_id, title, source, status)`, `kb_chunks (id, document_id, agent_id, content, embedding vector(N), metadata jsonb)` + HNSW index on `embedding`, btree on `agent_id`.
   - Ingestion runs as a background job: upload → store file (§6) → extract text → chunk → embed → insert.
   - At chat time: retrieve top-k chunks **filtered by `agent_id`** (tenant isolation), inject them into the prompt after the cached system prompt, and return source references in the response.
   - Embedding provider behind an interface; its key comes from env.

---

## 6. File Storage

- Store all uploads (KB documents, future attachments) in S3-compatible object storage (S3 / Cloudflare R2 / GCS). Never in Postgres or on the container disk.
- Upload flow: API issues a **pre-signed PUT URL** (short expiry, content-type + size limits) → client uploads directly → API records metadata → background job processes the file.
- Downloads via short-lived pre-signed GET URLs. Bucket is private; server-side encryption on.
- Optional malware scan step before processing.

```dotenv
STORAGE_BUCKET=<provided separately>
STORAGE_ENDPOINT=<provided separately>
STORAGE_ACCESS_KEY_ID=<provided separately>
STORAGE_SECRET_ACCESS_KEY=<provided separately>
```

---

## 7. Organizations, RBAC & Row-Level Security

1. Tables: `organizations`, `org_members (org_id, user_id, role)`, roles `owner | admin | member | viewer`.
2. Agents can be public (current behavior) or org-private (`agents.org_id` nullable).
3. Authorization in one policy module (`app/core/authz.py`), e.g. `require_role(org, "admin")`. No role checks scattered across routers.
4. **Defense in depth:** enable Postgres Row-Level Security on `messages`, `conversations`, and `kb_chunks`. The app sets `SET LOCAL app.user_id = ...` / `app.org_id` per transaction; policies restrict rows to that scope. Migrations and admin jobs use a separate DB role with `BYPASSRLS`.
5. Tests: cross-org access is denied at both the app layer and the RLS layer.

---

## 8. Platform & Integration

1. **API Gateway / edge** (Cloudflare, Kong, or the cloud provider's gateway): TLS, WAF, DDoS protection, global IP rate limits, request size limits. The app still enforces its own auth and limits.
2. **Webhooks for B2B clients:** `webhook_endpoints` table (url, secret, events). Deliveries go through the outbox. Payloads are HMAC-SHA256-signed (`X-AgentHub-Signature`, timestamp header, 5-min replay window). Retries with backoff, delivery log, manual re-send.
3. **i18n:** `Accept-Language` / user preference (`users.locale`). Error messages come from translation files (English + Bangla to start). The agent reply language follows the user's preference through a prompt instruction.
4. **SDKs & docs:** generate TypeScript and Python clients from the OpenAPI spec in CI; publish a changelog; add **contract tests** that fail CI on breaking schema changes (e.g. `oasdiff`).

---

## 9. Resilience, DR & Operations

1. **Disaster recovery:** document RPO ≤ 5 min and RTO ≤ 1 h. PITR enabled; backups copied to a second region; quarterly restore drill with the result recorded.
2. **Failure testing** (staging): kill the replica, Redis, the queue, and the LLM (mock the outage) one at a time; verify the app degrades as described in §1.2, §2.3, and §3. Automate these as integration tests where possible.
3. **Runbooks** in `docs/runbooks/`: LLM outage, primary failover, replica lag, Redis down, queue backlog, leaked credential, cost spike. Each has: symptoms, dashboards/alerts, immediate action, rollback, follow-up.
4. **Capacity signals** (alerts): primary CPU > 70 %, replica lag > 2 s, DB pool > 80 %, Redis memory > 75 %, outbox backlog > 1,000 or oldest > 60 s, cache hit rate < 70 % on the catalog.
5. **Multi-region (later):** stateless app in 2 regions behind global load balancing; primary DB in one region with a cross-region replica for failover. Only when latency or customer needs require it.

---

## 10. Implementation Order

| Step | Section | Trigger |
|---|---|---|
| 1 | §2 Multi-layer caching (CacheService, patterns, HTTP cache) | Now |
| 2 | §1 Read/write session split (replica **disabled**, both URLs = primary) | Now |
| 3 | §3 Transactional outbox + worker | Now |
| 4 | §5.1 SQL catalog search with `pg_trgm` | Now |
| 5 | §9.1–9.4 DR, failure tests, runbooks, alerts | Before GA |
| 6 | §1 Enable replica in production | Primary CPU > 60–70 % or reads ≫ writes |
| 7 | §4 Analytics separation | When admin reporting starts |
| 8 | §6 File storage + §5.2 RAG | When the knowledge-base feature is approved |
| 9 | §7 Organizations / RBAC / RLS | When B2B customers arrive |
| 10 | §8 Gateway, webhooks, i18n, SDKs | Per product demand |
| 11 | §9.5 Multi-region | Global latency / enterprise requirement |

---

## 11. Definition of Done

- [ ] Writer/reader sessions in place; reader is read-only; read-your-writes and the lag guard work; replica off = identical behavior
- [x] `CacheService` is the only cache entry point; versioned keys, jitter, stampede lock, SWR, negative cache, L1 + pub/sub invalidation
      — Implemented: this session — `app/services/cache.py`, `tests/test_cache_service.py` (hit/miss/invalidate, negative caching, 50-concurrent stampede → 1 load, SWR verified via a fakeredis smoke check — see `docs/PROGRESS.md`)
- [x] Redis outage → API still serves correct data (tested)
      — Implemented: this session — `tests/test_cache_service.py::test_redis_down_still_returns_correct_data`; live-verified `/agents`, `/agents/{slug}`, `/industries` (see `docs/PROGRESS.md`)
- [ ] Outbox: no lost events, idempotent consumers, dead-letter + alert
- [ ] No analytics queries on the primary
- [ ] Catalog search in SQL with a trigram index
- [ ] (If enabled) RAG retrieval is always filtered by `agent_id`; files in private object storage via pre-signed URLs
- [ ] (If enabled) RBAC + RLS deny cross-org access at both layers
- [ ] Failure tests for replica / Redis / queue / LLM pass in staging
- [ ] Runbooks and alerts committed; DR restore drill done once
- [ ] All new env variables documented in `.env.example` with placeholders only
- [ ] `git grep` finds no credentials; all tests pass on PostgreSQL + Redis in CI

# AgentHub — Claude Code Execution Brief

> Save this file as `docs/CLAUDE_CODE_INSTRUCTIONS.md` in the repo, then start Claude Code in the repo root with the kickoff prompt at the bottom.

---

## 1. Your Role & Mission

You are the senior backend engineer on **AgentHub API** (FastAPI · SQLAlchemy async · Alembic · PostgreSQL).

Three instruction documents live in `docs/`:

| # | File | Scope |
|---|------|-------|
| 1 | `docs/AGENTHUB_FIX_INSTRUCTIONS.md` | Bug fixes P0 → P3 (F1–F18), PostgreSQL-only setup |
| 2 | `docs/AGENTHUB_IMPROVEMENT_ROADMAP.md` | Streaming, prompt caching, Redis limits, observability, security, compliance |
| 3 | `docs/AGENTHUB_SCALE_ARCHITECTURE.md` | Read/write split, multi-layer cache, outbox, pg_trgm, RAG, RBAC/RLS, DR |

**Step 1 (the Fix Instructions) has already been worked on by a previous session.**
Your job:

1. **Review** that work rigorously against doc #1 — do not trust it, verify it.
2. **Fix every gap** you find.
3. **Implement** docs #2 and #3 in the order defined below, until every Definition of Done you are able to satisfy in code is checked.

Read all three docs fully before writing any code. They are the source of truth; this brief only defines *how* to execute them.

---

## 2. Non-Negotiable Rules

1. **PostgreSQL only** — `postgresql+asyncpg://`. No SQLite in code, tests, CI, or fallbacks. Missing `DATABASE_URL` → fail at startup.
2. **No credentials anywhere in the repo.** Read secrets from env only. `.env` stays gitignored; `.env.example` has placeholders only. Never print secrets in logs, test output, commits, or your messages to me.
3. **If `.env` / credentials are missing, stop and ask me.** Never invent or hardcode them.
4. **Every schema change = Alembic migration** (zero-downtime: expand → backfill → contract). No `create_all()` outside tests.
5. **Every change has a test.** An item is *not done* until a test proves it and the full suite is green.
6. **Every new feature sits behind an env toggle** and falls back to current behavior when off or when its dependency (Redis, replica, queue) is down.
7. **No public API contract changes** unless a doc explicitly says so. The non-streaming `POST /chat` must keep working.
8. **Small, atomic commits** — one per finding/section:
   - `fix(F1): ...` for doc #1
   - `feat(roadmap-§3): ...` for doc #2
   - `feat(scale-§2): ...` for doc #3
   End each commit message with the co-author attribution configured in this environment.
   Commit locally. **Do not push or open PRs unless I ask.**
9. Don't delete or rewrite unrelated code. If something outside scope looks broken, log it in `docs/PROGRESS.md` under "Out-of-scope findings" and move on.
10. **Never run destructive commands** (`alembic downgrade`, `DROP`, `TRUNCATE`, reset scripts) against `DATABASE_URL`. Use only `TEST_DATABASE_URL` or a throwaway DB. Always print the target DB name (never credentials) before any migration command.
    - This is a lesson from a real incident (see `docs/PROGRESS.md`, M0 audit): a manual `alembic downgrade base && alembic upgrade head` verification step was run without `ALEMBIC_DATABASE_URL` set and silently wiped the local dev database.
    - Code guard: `app/core/db_safety.py` (`refuse_if_main_db`) refuses to proceed whenever a test or migration-check script's target database name equals `DATABASE_URL`'s database name. It is wired into `tests/conftest.py` (session-start guard) and `scripts/verify_migration_cycle.py` (the safe replacement for ad hoc downgrade/upgrade verification). Covered by `tests/test_db_safety_guard.py`.

---

## 3. Working Loop (for every item)

```
Read the doc section  →  Inspect current code  →  Plan (short)  →  Implement
   →  Write/extend tests  →  Run full suite on Postgres  →  Lint  →  Commit
   →  Update docs/PROGRESS.md
```

Standard commands (adjust if the repo differs — record the actual ones in `PROGRESS.md`):

```bash
docker compose up -d postgres redis          # test services (add a compose file if missing)
alembic upgrade head
pytest -q                                    # uses TEST_DATABASE_URL, never the main DB
ruff check . && ruff format --check .
mypy app                                     # once type checking is introduced
```

Use subagents for independent, read-heavy work (e.g. auditing a doc section while you implement another). Use plan mode before any multi-file refactor (session split, cache layer, outbox).

---

## 4. Progress Tracking (mandatory)

Create and maintain `docs/PROGRESS.md`. It is how the next session resumes if this one ends.

```markdown
# AgentHub Progress

## Current milestone
M2 — Roadmap Phase 1

## Status
| ID | Item | Status | Commit | Test(s) | Notes |
|----|------|--------|--------|---------|-------|
| F1 | Message ordering (seq) | ✅ verified | abc123 | tests/test_history_order.py | backfill tested |
| F2 | LLM error → 503 | 🔧 fixed in review | def456 | ... | was missing Retry-After |
| roadmap-§3 | Streaming SSE | ⏳ in progress | | | |

Legend: ✅ done & verified · 🔧 fixed during review · ⏳ in progress · ⛔ blocked · 🕒 deferred (trigger not met) · 📄 docs/manual only

## Out-of-scope findings
## Decisions & assumptions
## Needs human action
```

At the start of every session: read `docs/PROGRESS.md` first and continue from the first unfinished item.

---

## 5. Milestones (execute in this order)

### M0 — Review Step 1 (Fix Instructions) · *do this first*

Audit, don't assume. For **each** of F1–F18 and §1 (PostgreSQL setup), §6 (Tests & CI):

1. Locate the implementation (file + line) and the test that proves it.
2. Classify: **Done & correct** / **Partially done** / **Missing** / **Done but wrong**.
3. Run these checks and record results:
   ```bash
   git log --oneline | head -50
   git grep -niE "sqlite|aiosqlite"                 # must be empty
   git grep -niE "password|postgresql://|sk-ant"    # placeholders only
   grep -n "sqlalchemy.url" alembic.ini             # must be blank
   alembic downgrade base && alembic upgrade head   # clean on empty DB
   pytest -q                                        # all green (≥ 29 original tests + new ones)
   ```
4. Verify the F1 migration **backfill** on a DB that already contains messages with identical `created_at` (seed some, then migrate).
5. Hit all 10 endpoints live against Postgres (happy + error paths) and note results.
6. Write the audit table into `docs/PROGRESS.md`, then **send me a short summary** (what was fine, what was broken, what you'll fix).
7. Fix every gap with a `fix(Fn): ...` commit + test. P3 items (F12–F18) are in scope — do them.

**Exit criteria:** every checkbox in doc #1 §7 is ticked with evidence.

### M1 — Scale doc prerequisites that the roadmap depends on

Redis is needed from here on. Add it to compose/CI, add `REDIS_URL` to settings and `.env.example`, and make the app boot fine without it (toggles off).

### M2 — Roadmap Phase 1 (doc #2)

- §3 Chat streaming (SSE) + `messages.status` migration
- §4.1 Anthropic prompt caching (verify current minimum cacheable length in Anthropic docs)
- §5 Redis rate limiting, daily token quota, `X-RateLimit-*` headers, login lockout

### M3 — Scale "Now" items (doc #3 §10 steps 1–4)

- §2 Multi-layer caching via a single `CacheService` (this supersedes roadmap §4.2; implement once, to the scale-doc spec) + HTTP ETag/304 (roadmap §4.3)
- §1 Reader/writer session split — replica **disabled**, both URLs = primary, reader is read-only, read-your-writes, lag guard
- §3 Transactional outbox + relay worker (Redis Streams) + idempotent consumers + DLQ
- §5.1 Catalog search in SQL with `pg_trgm`

### M4 — Roadmap Phase 2

§6 Conversations entity + token-budget context + summaries/titles (via outbox/worker) · §7 resilience (timeouts, retries, circuit breaker, idempotency keys, graceful shutdown) · §8 `/v1` versioning + RFC 9457 errors + cursor pagination

### M5 — Roadmap Phase 3

§9 observability (JSON logs, request IDs, OTel, `/metrics`, Sentry) · §10 security (refresh tokens, revocation, email verification, password reset, security headers, CI scanners) · §11 data (soft delete, audit_log, `llm_usage`) · §12 CI/CD pipeline, multi-stage non-root Dockerfile, k6/Locust scripts

### M6 — Roadmap Phase 4 + Scale "Before GA"

Roadmap §13–§16 (export/delete, retention job, moderation hook, guardrails, `agent_versions`, eval suite skeleton, cost controls, admin API) · Scale §9.1–9.4 (failure tests, runbooks in `docs/runbooks/`, alert definitions)

### M7 — Trigger-based scale items (doc #3 §10 steps 6–11)

These have business triggers (replica load, KB approval, B2B customers). **Build the code behind disabled flags** where reasonable — §4 analytics consumer, §6 file storage + §5.2 RAG (`RAG_ENABLED=false`), §7 orgs/RBAC/RLS, §8 webhooks/i18n/SDK generation. Mark §9.5 multi-region as 🕒 deferred (docs only).
**Ask me before starting M7** — I may want to reprioritize.

---

## 6. What You Cannot Do in Code

Some DoD items are infrastructure or process (managed Postgres failover, PgBouncer deployment, CDN/WAF, pen test, DR restore drill, secret manager rotation, production load test). For these:

- Produce the artifact that makes them easy: config, IaC/blueprint snippet, runbook, script, checklist.
- Mark them 📄 in `PROGRESS.md` under **Needs human action** with exact next steps.
- Never claim they are done.

---

## 7. When to Stop and Ask Me

- Credentials / `.env` values are missing or a service won't connect.
- A doc instruction conflicts with the existing code, another doc, or itself (state both options + your recommendation).
- A change would break the public API contract.
- A migration would rewrite or lock a large table, or would lose data.
- Before starting M7.
- Tests fail for a reason you can't resolve after two focused attempts.

Otherwise, keep going — don't ask for permission on routine decisions; log them under "Decisions & assumptions".

---

## 8. Reporting

At the end of **each milestone**, send me:

1. Milestone name and ✅/⛔ status
2. Commits made (one line each)
3. Test count before → after, and suite status
4. Anything blocked or needing human action
5. What's next

Keep it short. Details belong in `docs/PROGRESS.md`.

---

## 9. Final Definition of Done

- Every checkbox in doc #1 §7, doc #2 §18 and doc #3 §11 is either ✅ with evidence (commit + test) or 📄/🕒 with a clear reason and next step.
- `pytest` green on PostgreSQL + Redis; lint clean; `alembic upgrade head` clean on empty and populated DBs.
- `git grep` finds no credentials and no SQLite.
- `.env.example` documents every env var (placeholders only).
- `docs/PROGRESS.md` is complete and accurate.

---

## Kickoff Prompt (paste into Claude Code)

```text
Read docs/CLAUDE_CODE_INSTRUCTIONS.md and follow it exactly.
Then read docs/AGENTHUB_FIX_INSTRUCTIONS.md, docs/AGENTHUB_IMPROVEMENT_ROADMAP.md
and docs/AGENTHUB_SCALE_ARCHITECTURE.md in full.

Start with Milestone M0: audit the already-completed Step 1 (Fix Instructions)
against its Definition of Done, create docs/PROGRESS.md with the audit table,
send me a short summary, then fix every gap. After M0 is green, continue
milestone by milestone. Commit locally per item; do not push.
```

**Resume prompt** (for a new session):

```text
Read docs/CLAUDE_CODE_INSTRUCTIONS.md and docs/PROGRESS.md, then continue
from the first unfinished item.
```

# Runbook: transactional outbox (scale-doc §3)

Covers the failure modes the outbox relay/consumer introduce. No credentials
in this file.

## 1. Events piling up unpublished (`outbox_events` growing, `published_at IS NULL`)

**Check first:**
```sql
SELECT event_type, count(*), min(created_at), max(attempts)
FROM outbox_events WHERE published_at IS NULL
GROUP BY event_type;
```

**Likely causes:**
- **Redis is down or unreachable.** The relay logs
  `redis unavailable, degrading: <Type>` (WARNING, `agenthub.redis`). Events
  stay safely queued (never lost — that's the point of the outbox) and get
  published automatically once Redis recovers; the relay retries every
  `OUTBOX_RELAY_INTERVAL_SECONDS`. See `docs/runbooks/redis-degradation.md`.
- **`OUTBOX_ENABLED=false`.** The relay loop is a no-op; nothing will ever
  publish until it's turned back on. New events also stop being written
  once it's off (not just the relay), so this shouldn't silently
  accumulate rows — check the setting first if the table is growing.
- **The app process isn't running / the lifespan background tasks didn't
  start.** The relay only runs inside the FastAPI app's lifespan
  (`app/main.py`); running only `alembic`/scripts doesn't relay anything.

**What NOT to do:** don't manually `UPDATE outbox_events SET published_at =
now()` to "clear" the backlog — that marks events as delivered without
actually publishing them, which is the "lost event" failure the whole
feature exists to prevent. Fix the underlying cause (usually: get Redis
back) and let the relay catch up.

## 2. An event ended up on the dead-letter stream

**Symptom:** log line `outbox event moved to DLQ after N attempts: type=...
id=... last_error=...` at ERROR (`agenthub.outbox.relay`) — this is the
"alert" scale-doc §3 point 4 calls for; there's no PagerDuty/Sentry
integration yet (that's roadmap §9, not built), so an ERROR-level log is
the current alerting mechanism.

**What to do:**
1. Read `last_error` in the log line (or `SELECT last_error FROM
   outbox_events WHERE id = '<id>'` — the row is marked `published_at`
   NOT NULL once DLQ'd, but the row itself isn't deleted).
2. Inspect the dead-letter stream directly:
   `XRANGE agenthub:{env}:outbox:<event_type>:dlq - +`.
3. Most DLQ causes are a sustained Redis outage across all
   `OUTBOX_MAX_ATTEMPTS` retries (5 by default, ~5 relay intervals apart) —
   if Redis was down that whole window, this is expected, not a bug.
4. There is no automatic DLQ replay yet. To reprocess by hand: read the
   entry's `payload` field from the DLQ stream and re-run the relevant
   handler logic manually, or `XADD` it back onto the normal stream if the
   underlying problem (e.g. Redis) is now fixed.

## 3. A `chat.completed` durable usage count looks wrong

- **Under-counting after a Redis outage**: events queued during the outage
  are still in `outbox_events` (unpublished) or the DLQ — they aren't lost,
  but they also haven't incremented the counter yet. Reprocess the DLQ (§2)
  if any landed there.
- **A message the app never saw won't have an event.** Only turns that
  reach `final_status == "complete"` emit `chat.completed` — interrupted
  (client disconnect) and failed (LLM error) turns don't, by design (see
  `app/services/chat_engine.py`).
- **Restarting the app does *not* reset this counter** (unlike the
  in-process `UsageTracker` behind `GET /meta/usage`) — it's a plain Redis
  key (`agenthub:{env}:usage:{agent_slug}`), durable until Redis itself
  loses it.

## 4. Verifying the relay/consumer yourself

```bash
# with the app running (mock LLM is fine) and Redis up:
curl -X POST http://localhost:8000/agents/<slug>/signup -d '...'   # writes user.signed_up
curl -X POST http://localhost:8000/agents/<slug>/chat -d '...'      # writes chat.completed

# within ~OUTBOX_RELAY_INTERVAL_SECONDS, check it published:
redis-cli -u redis://127.0.0.1:6380/0 XLEN "agenthub:development:outbox:chat.completed"

# and that the consumer processed it:
redis-cli -u redis://127.0.0.1:6380/0 GET "agenthub:development:usage:<slug>"
```

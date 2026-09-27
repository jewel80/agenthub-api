# Runbook: reader/writer session split (scale-doc §1)

## 1. "current transaction is read-only" error from an endpoint

**Symptom:** a request fails with a Postgres error whose message contains
`cannot execute ... in a read-only transaction` (surfaced as a 500 unless
the route wraps it).

**Cause:** the route is using `get_read_db` / `get_read_db_for_user`
(`app/core/db.py`) but tries to write (INSERT/UPDATE/DELETE/DDL). Every
reader session runs with `default_transaction_read_only = on` — this is
intentional (scale-doc §1.2.3): a misrouted write must fail loudly, in
tests and in prod, rather than silently succeed against the wrong node.

**Fix:** the route should depend on `get_db` (writer) instead. Check the
routing table in `docs/AGENTHUB_SCALE_ARCHITECTURE.md` §1.2 step 4 — writes,
and any read that must reflect the caller's own very-recent write, belong
on the writer (or `get_read_db_for_user`, which routes to the writer
automatically during the read-your-writes window).

## 2. Replica marked unhealthy / reads not hitting the replica

Only relevant once `DB_READ_REPLICA_ENABLED=true` and `DATABASE_READ_URL`
point at a real streaming replica — the shipped default has no replica, so
this section doesn't apply yet.

- `run_replica_lag_guard()` (`app/core/db.py`) checks
  `pg_last_xact_replay_timestamp()` on the replica every 5s. Above
  `DB_REPLICA_MAX_LAG_SECONDS`, or on any connection error, it marks the
  replica unhealthy and `get_read_db` fails over to the primary until a
  later check succeeds again.
- **What to do:** check the replica's actual replication lag/connectivity
  at the Postgres level first (this is a real signal, not a false
  positive, unless the replica is reachable and caught up). While
  unhealthy, reads correctly go to the primary — no action needed to keep
  the API correct, only to restore replica capacity.

## 3. A user doesn't see their own message right after posting it

Only possible once a replica is enabled (with no replica, "the reader" is
already the primary, so this can't happen). Checklist:

- Is `REDIS_URL` configured and reachable? Read-your-writes
  (`mark_read_your_writes`/`read_your_writes_active`) needs Redis to work
  once a replica exists — without it, the mechanism fails *safe* (routes to
  the writer) only while Redis is reachably down, not while it's simply
  unconfigured. See `docs/PROGRESS.md` M3 §1 notes.
- Is `READ_YOUR_WRITES_WINDOW_SECONDS` long enough for the actual replica
  lag under load? It's a fixed window, not lag-aware — if replica lag can
  regularly exceed it, raise it or rely on the lag guard's own fail-over
  instead.

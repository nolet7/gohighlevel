# ADR 0002: SQLite, single instance, DB-level guards

**Status:** accepted for v1; revisit at ~5 tenants or any HA requirement

**Decision.** Persist enrollments, contact flags, the message log, idempotency keys and the audit trail in SQLite
(WAL mode). Run one process with one worker. All "do exactly once" guarantees are database constraints:
`UNIQUE` active enrollment per (contact, sequence); `seen_events` for webhook and cron run keys; `UNIQUE` message
idempotency keys.

**Why.** Zero operational dependencies; a restart loses nothing; the file is trivially backed up.

**Consequences.** No horizontal scaling. At-least-once delivery to GHL: if the process dies between the provider call
and recording the message, a retry may re-send once (the GHL client keeps a best-effort in-memory dedupe).

**Migration path.** `Store` is the only class touching SQL and uses plain statements. Port to Postgres by
reimplementing it; the partial unique index and `INSERT … ON CONFLICT` translate directly.

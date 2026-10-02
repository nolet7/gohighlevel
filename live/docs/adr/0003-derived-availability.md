# ADR 0003: Availability derived from jobs and configured capacity

**Status:** accepted; the main accuracy risk of the calendar-fill feature

**Context.** The headline feature needs "open slots 7–14 days out". The FieldPulse API reference lists customers,
jobs, estimates, invoices, users, payments, timesheets, teams, tags and more, but **no schedule, appointment or
availability endpoint**.

**Decision.** `open = capacity(day) − jobs scheduled that day (excluding cancelled)`, where
`capacity = HGE_FP_TECH_COUNT × HGE_FP_SLOTS_PER_TECH_PER_DAY` on `HGE_FP_WORKDAYS`.

**Known inaccuracies (accepted for v1).**
- Ignores technician time off, job duration and per-tech skills.
- Treats every job as one slot.
- Reads all jobs and filters client-side (the documented `filter` syntax wasn't available to us).

**Mitigations.** Anomaly guard (aborts when the calendar looks implausibly empty), dry-run default for scheduled
runs, hard `fill_max_targets` cap, weekly per-contact cap.

**Upgrade path (in order of effort).** (1) per-weekday capacity table; (2) subtract technician time off via
`users`/`teams`; (3) weight jobs by estimated duration; (4) if FieldPulse ships an availability API, replace
`FieldPulseClient.availability` only.

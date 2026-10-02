# Architecture

## Context

    Lead sources                 Phone                     Technicians / office
    (web form, Meta, LSA)        (missed calls)            (FieldPulse app)
          │ signed webhook            │                          │
          ▼                           ▼                          ▼
    ┌──────────────────────────────────────────┐        ┌────────────────┐
    │ GoHighLevel                              │        │  FieldPulse    │
    │  contacts · SMS/email · pipeline · AI    │        │  customers     │
    │  workflows (RVM, Sendblue via tags)      │        │  jobs · est.   │
    └───────────────┬──────────────────────────┘        │  invoices      │
                    │ signed webhooks (inbound msg,      └───────┬────────┘
                    │ missed call, lead)                         │ REST (x-api-key)
                    ▼                                            │ job-status webhook (a HINT)
    ┌───────────────────────────────────────────────────────────┴───────┐
    │                      HVAC Growth Engine                           │
    │  api ─► services (intake · conversation · runner · sync ·         │
    │         campaigns) ─► ports ─► adapters (ghl · fieldpulse · mock) │
    │  compliance policy · SQLite store · scheduler · metrics           │
    └───────────────────────────────────────────────────────────────────┘

**Responsibility split.** FieldPulse owns customers, jobs, estimates, invoices and the schedule. GHL owns
conversations, pipeline stages and message delivery. The engine owns *decisions*: who to message, when, whether
they're allowed to be messaged, and when to stop.

## Layers

| Layer | Rule |
|---|---|
| `domain.py`, `ports.py` | Pure types and `Protocol` interfaces. No I/O. |
| `services/*` | Business logic. Depends only on ports, store, clock, policy. |
| `adapters/*` | The only code that knows GHL/FieldPulse wire formats. Swappable. |
| `container.py` | Composition root. Only place that picks adapters by `HGE_MODE`. |
| `api.py` | HTTP surface: auth, validation, idempotency. No business logic. |

## Key flows

**Lead intake.** normalize phone/email → find or create FieldPulse customer → upsert GHL contact → store consent
flag → enroll `speed_to_lead` unless FieldPulse already shows open work → fire delay-0 steps immediately.

**Sequence execution.** Each tick, for every active enrollment and every due step: re-check FieldPulse state
(defense in depth) → ask the compliance policy (`ALLOW / DEFER / SKIP / STOP`) → send with an idempotency key →
record → advance. Delays are measured from the previous send.

**Stop-on-convert.** `sync` compares FieldPulse state to the stored state. On any of booked / converted /
completed / paid it cancels every marketing sequence (the review request is the only exception: it is *triggered*
by payment). The runner re-checks the stored state before every send, so a missed sync still cannot market to a
booked customer.

**Calendar-fill.**

    availability(today+7 .. today+14) from FieldPulse
      → slow days = workdays with ≥ 50% of capacity open
      → anomaly guard: > 75% of workdays slow ⇒ abort (likely bad data)
      → budget = min(max_targets, ceil(ceil(open_slots × fill_fraction) / expected_booking_rate))
      → candidates = opted-in past customers, last service ≥ 6 months ago, oldest first
      → skip: no opt-in · DND · enrolled · weekly cap · open work in FieldPulse
      → enroll `fill_calendar` (SMS + email naming the specific slow days and the offer)

## FieldPulse integration facts (from official docs, verified Oct 2026)

- Base URL + `x-api-key` header; key issued by FieldPulse support on request.
- `GET /jobs|/customers|/estimates|/invoices`, `page` from 1, `limit` ≤ 100, envelope `{error,total_count,response}`.
- 50 requests/second; 429 with `RateLimit-Reset` (epoch). Honoured by `adapters/http.py`.
- Webhooks exist for job status only (no signature scheme documented ⇒ treated as hints).
- **No schedule or availability endpoint.** Availability is derived: configured capacity (techs × slots × workdays)
  minus jobs scheduled that day. The engine is only as accurate as that capacity model. See ADR 0003.

## Scaling

Single process, single SQLite file: appropriate for one home-service company. For multi-tenant (many clients) or HA,
replace `store.py` with Postgres (SQL is plain, one class) and run the scheduler as a single leader or via external
cron against `/admin/jobs/*`. Run guards live in the database, so double-firing is safe. See ADR 0002.

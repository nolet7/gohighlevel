# HVAC Lead Follow-Up + FieldPulse Sync: prototype and build plan

## Run the demo
    cd hvac_ghl && python3 demo.py        # 9 steps, 18 assertions, offline

Demo order for the team (about 10 min): lead intake -> missed-call text-back -> AI booking ->
follow-up sequence that stops on booking -> FieldPulse status sync -> estimate reactivation ->
**fill-the-calendar campaign** (the headline feature) -> STOP/opt-out -> database reactivation.

## Architecture
    Ad/Web/LSA/Phone --> GHL (contacts, workflows, Conversation AI, SMS/email)
                              ^  |  webhooks / API
                              |  v
                         Middleware (n8n / Make / small cloud function)
                              ^  |
                              |  v
                         FieldPulse (customers, jobs, estimates, invoices, schedule)

- `adapters.py`: MockGHL / MockFieldPulse. In production replace with real clients exposing the same methods.
- `engine.py`: the logic. Intake normalizer, AI qualification, sequences, sync/stop rules, calendar-fill.

## Prototype -> live mapping
| Prototype | Live build |
|---|---|
| `ingest_lead` | GHL: Website form trigger, Facebook Lead Form trigger, LSA via Zapier/Make or GHL's Google LSA integration; all feed one "New Lead" workflow |
| `missed_call` | GHL built-in Missed Call Text Back |
| `handle_inbound` | GHL Conversation AI bot (qualification prompt + Appointment Booking action); `urgent` keywords become bot rules |
| `SEQUENCES` | GHL workflows with Wait steps; exit condition = tag `fp:booked/converted/completed/paid` |
| `sync_from_fieldpulse` | Middleware poll (every 5-10 min) or FieldPulse webhooks if offered; writes tags + pipeline stage to GHL |
| `fill_calendar` | Scheduled middleware job (daily 7am): read FieldPulse schedule -> compute open slots 7-14 days out -> add tag `campaign:fill-slow-days` to a sized, filtered segment -> GHL workflow sends the offer |
| Ringless voicemail / Sendblue | Extra channel steps inside the same workflows (GHL RVM add-on; Sendblue via webhook action). Same stop rules apply |

## Key rules baked in (and tested)
- Any FieldPulse state of booked / converted / completed / paid stops every marketing sequence.
- STOP sets DND and halts everything; DND contacts are never targeted.
- Campaigns are sized to open capacity, longest-since-service customers first.
- Contacts with open work in FieldPulse are never marketed to.
- Review request fires on payment only.

## What is NOT verified yet (needs access)
1. **FieldPulse API**: I have not tested against a live account. Day-1 task is to confirm which endpoints
   (customers, jobs, estimates, invoices, schedule/availability) and webhooks the company's plan exposes.
   If schedule data is not exposed, fallbacks are: Zapier/Make FieldPulse triggers, a calendar feed,
   or a nightly export. This is the main project risk and decides how real-time the calendar logic can be.
2. GHL pieces (Conversation AI tuning, A2P 10DLC registration, RVM, LSA connection) must be configured in the real sub-account.
3. SMS compliance: A2P 10DLC approval, consent language on forms, quiet hours (add 8am-9pm local send window).
4. Availability in the mock is a simple slots/tech model; real capacity should come from FieldPulse tech schedules.

## Suggested rollout
Week 1 access + FieldPulse API discovery + A2P registration. Week 2 lead intake, missed-call, AI booking.
Week 3 sync + stop rules. Week 4 reactivation + calendar-fill (start with one segment, human review of first send).

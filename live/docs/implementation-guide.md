# Implementation guide: from this repo to live production

Follow in order. Each phase has an **exit check**; don't start the next phase until it passes.
Background and troubleshooting live in [runbook.md](runbook.md); rationale in [architecture.md](architecture.md).

> UI labels in GoHighLevel and Meta change often. Where this guide names a screen or merge field, confirm it in the
> product before relying on it. Those spots are marked **(confirm)**.

---

## Phase 0: Get the code running locally (30 min)

1. Clone and install:

       git clone https://github.com/nolet7/gohighlevel.git && cd gohighlevel/live
       python -m venv .venv && source .venv/Scripts/activate      # Windows Git Bash
       pip install -e ".[dev]"

2. `python -m pytest` → **139 passed**. `python -m hvac_engine.demo` → **22/22 checks passed**.
3. `cp .env.example .env`, then set three real secrets (each `python -c "import secrets; print(secrets.token_urlsafe(32))"`):
   `HGE_WEBHOOK_SECRET`, `HGE_ADMIN_API_KEY`, `HGE_FIELDPULSE_WEBHOOK_TOKEN`.
4. `uvicorn hvac_engine.main:app --port 8000` and `curl localhost:8000/readyz` → `{"status":"ready","mode":"mock"}`.

**Exit check:** tests green, demo green, `/readyz` ready.

## Phase 1: Accounts, keys and approvals (start now; these take days)

| Item | How | Lead time |
|---|---|---|
| FieldPulse API key | Email support@fieldpulse.com or use in-app chat and ask for an **API key** | days |
| GHL sub-account (location) | Create or choose the client's sub-account; note the **Location ID** | same day |
| GHL Private Integration token | Settings → Private Integrations **(confirm)**. Grant read+write on contacts, conversations/messages, opportunities | same day |
| A2P 10DLC registration | Register brand + campaign in GHL (LeadConnector phone settings) **(confirm)**. **No marketing SMS goes out until approved.** | 1-3 weeks |
| Counsel review | Send `security-compliance.md` to the client's attorney; get written answers to its five open items | days |
| Hosting | Any container host with a **persistent disk** (SQLite lives there): a small VPS, Fly.io, Render | same day |

**Exit check:** you hold the FieldPulse key, GHL token + Location ID, and A2P is submitted.

## Phase 2: Verify the integrations against the real accounts (1-2 hours)

1. **GHL probe** (creates one test contact tagged `hge-probe`, sends nothing unless you pass a number):

       HGE_GHL_TOKEN=... HGE_GHL_LOCATION_ID=... python scripts/ghl_probe.py
       # add  --send-sms-to +1YOURMOBILE  to test one real message (A2P must be approved)

   Copy the printed `HGE_GHL_API_VERSION=` value into `.env`.

2. **FieldPulse probe** (read-only):

       HGE_FIELDPULSE_API_KEY=... python scripts/fieldpulse_probe.py

   It lists the real JSON keys and flags every field the adapter expects but can't find.
   Fix mismatches in the `FieldMap` dataclass at the top of `src/hvac_engine/adapters/fieldpulse.py`, re-run until
   it prints **ALL CHECKS PASSED**. Also set the status vocabulary (what "completed", "cancelled", "approved", "paid"
   are literally called in this account).

3. Confirm the two POST shapes (`create_lead`, `create_job`) in FieldPulse's Postman collection
   (https://documenter.getpostman.com/view/35988189/2sA3XLEjFd), then create one **test customer and test job** through
   the engine against a sandbox/test customer record and delete them afterward.

4. Add contract tests for anything you changed in `FieldMap` (copy the pattern in `tests/test_adapters.py`) and run
   `python -m pytest`.

**Exit check:** both probes pass; tests still green.

## Phase 3: Model the schedule (1 hour with dispatch)

FieldPulse has no availability API, so open slots = configured capacity − scheduled jobs (ADR 0003).

1. Ask dispatch: how many technicians run service calls, how many jobs each takes per day, which weekdays are bookable.
2. Set `HGE_FP_TECH_COUNT`, `HGE_FP_SLOTS_PER_TECH_PER_DAY`, `HGE_FP_WORKDAYS` (Mon=0 … Sun=6).
3. Run `POST /admin/jobs/fill-calendar?dry_run=true` (Phase 5 shows how) and compare the reported slow days with what
   dispatch says looks light. Adjust until they agree.

**Exit check:** dispatch agrees the dry-run's slow days are the real slow days.

## Phase 4: Configure GoHighLevel

1. **Pipeline.** Create a pipeline with exactly these stages: `New Lead`, `Booked`, `Won - Install`, `Job Complete`,
   `Paid / Review Request`. Put the IDs in `.env`:

       HGE_GHL_PIPELINE_ID=<id>
       HGE_GHL_STAGE_IDS={"New Lead":"<id>","Booked":"<id>","Won - Install":"<id>","Job Complete":"<id>","Paid / Review Request":"<id>"}

   (If you skip this, stages are written as `stage:<name>` tags instead.)

2. **Ringless voicemail and iMessage (optional).** Two small workflows:
   - Trigger "Tag added = `trigger:rvm`" → ringless voicemail action.
   - Trigger "Tag added = `trigger:imessage`" → webhook to Sendblue **(confirm Sendblue's current API)**.
   The engine only adds the tag; the workflow does the send.

3. **Inbound replies.** Workflow: trigger "Customer Replied" (SMS) → action "Webhook", `POST` to
   `https://YOUR-HOST/webhooks/ghl/inbound`, custom header `X-Webhook-Token: <HGE_WEBHOOK_STATIC_TOKEN>`, JSON body:

       {"contact_id": "{{contact.id}}", "message": "{{message.body}}"}      (confirm merge-field names)

   GHL's webhook action can't compute HMAC signatures, so enable the static-token mode in `.env`
   (`HGE_WEBHOOK_STATIC_TOKEN=<random>`). To keep replay protection, route this through Make/n8n and sign instead.

4. **Missed calls.** Workflow: trigger on a missed / no-answer call → webhook to `/webhooks/ghl/missed-call` with
   `{"phone": "{{contact.phone}}", "name": "{{contact.first_name}}"}`. Turn **off** GHL's own Missed Call Text Back so
   callers aren't texted twice.

5. **Lead sources → engine.**

   | Source | Wiring |
   |---|---|
   | Meta Lead Ads | Connect Facebook in GHL; workflow trigger "Facebook Lead Form Submitted" → webhook to `/webhooks/leads/meta` |
   | Google LSA | Connect LSA to GHL if available, or via Zapier/Make → webhook to `/webhooks/leads/lsa` |
   | Website form | Form → Make/n8n (signs the request) → `/webhooks/leads/website`. Include the consent checkbox as `sms_consent` |

   Lead body: `{"name","phone","email","issue","sms_consent"}`. **`sms_consent` must reflect a real checkbox.**
   Leads without it still get email; texts are skipped by design.

6. **Turn off overlapping GHL automations** (old speed-to-lead workflows, duplicate nurture sequences) so customers
   don't get two follow-ups.

**Exit check:** a test reply and a test missed call both reach the engine (check `GET /admin/audit`).

## Phase 5: Deploy

1. Production `.env` (never commit it):

       HGE_ENV=prod
       HGE_MODE=live
       HGE_COMPANY_NAME=<client business name>
       HGE_TIMEZONE=<client IANA zone, e.g. America/Chicago>
       HGE_CAMPAIGNS_LIVE=false            # keep false until Phase 7
       HGE_SCHEDULER_ENABLED=true
       HGE_GHL_TOKEN=...  HGE_GHL_LOCATION_ID=...  HGE_GHL_API_VERSION=...
       HGE_FIELDPULSE_API_KEY=...
       # + the secrets from Phase 0 and the schedule/pipeline settings from Phases 3-4

   `HGE_ENV=prod` refuses to start with placeholder secrets; `HGE_MODE=live` refuses to start without credentials.

2. Run it (single worker on purpose):

       docker compose up -d --build

   Put it behind HTTPS (host-provided TLS or Caddy/nginx). Mount `/data` on a persistent disk and **back it up daily**
   (`sqlite3 engine.db ".backup backup.db"`).

3. Verify:

       curl https://YOUR-HOST/readyz                                  # {"status":"ready","mode":"live"}
       python scripts/sign_webhook.py https://YOUR-HOST/webhooks/leads/website \
         '{"name":"Test Lead","phone":"YOUR-MOBILE","email":"you@example.com","sms_consent":true}'

   You should receive an SMS and an email within seconds, and a new `New Lead` contact should appear in GHL and
   FieldPulse. Reply "yes book me": a job should appear in FieldPulse and the contact should move to `Booked`.

4. Alerts: scrape `/metrics` (restrict it to your network) and alert on `hge_send_failures_total`,
   `hge_runner_errors_total`, `hge_sync_errors_total`, `hge_booking_failures_total` and `hge_campaign_blocked_total`.

**Exit check:** the end-to-end test lead above works in production.

## Phase 6: Shadow week (dry-run only)

Leave `HGE_CAMPAIGNS_LIVE=false`. The scheduler *plans* each morning but sends nothing.

- Daily: `POST /admin/jobs/fill-calendar?dry_run=true` and read the plan (slow days, candidate count, skip reasons).
- Confirm with dispatch that the slow days are right.
- Spot-check the skip reasons: opted-out and no-opt-in customers must appear under `skipped`.
- Lead intake, replies and booking are live from day one; only the campaigns are held back.

    curl -X POST "https://YOUR-HOST/admin/jobs/fill-calendar?dry_run=true" -H "Authorization: Bearer $HGE_ADMIN_API_KEY"

**Exit check:** five days of plans that dispatch agrees with, and counsel's written sign-off.

## Phase 7: First live campaign

1. Set `HGE_FILL_MAX_TARGETS=10`, `HGE_CAMPAIGNS_LIVE=true`, restart.
2. The next 9 AM run texts at most 10 past customers. Watch replies, opt-outs and bookings the same day.
3. Measure **bookings ÷ messages sent**. Put the real number in `HGE_FILL_EXPECTED_BOOKING_RATE`; that value sizes
   every future campaign.
4. Raise `HGE_FILL_MAX_TARGETS` in steps (10 → 25 → 40) over a few weeks, only while opt-out rate and complaints stay low.

## Rollback

| Problem | Action |
|---|---|
| Campaign misbehaving | `HGE_CAMPAIGNS_LIVE=false`, restart. Planning continues, sending stops. |
| Any wrong message pattern | `docker compose stop` (lead webhooks will 5xx; GHL/Make retry). Then fix. |
| Bad FieldPulse mapping | Stop; fix `FieldMap`; `POST /admin/jobs/sync`. |
| Customer says STOP is ignored | Check `GET /admin/audit` for their `opt_out` row; set DND manually in GHL; investigate. |

## Definition of done

- [ ] Both probes pass; tests green; A2P approved; counsel sign-off recorded
- [ ] End-to-end test lead works in production
- [ ] Five dry-run days agreed with dispatch
- [ ] First live campaign reviewed by a human; booking rate recorded
- [ ] Alerts firing to a real channel; daily DB backup verified by a test restore

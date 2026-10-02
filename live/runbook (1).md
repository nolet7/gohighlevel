# Runbook

## 1. Go-live checklist (in order)

| # | Step | Owner | Done when |
|---|---|---|---|
| 1 | Request a FieldPulse API key (support@fieldpulse.com or in-app chat) | Client | Key received |
| 2 | Register A2P 10DLC brand + campaign in GHL | Client + us | Campaign approved |
| 3 | Create a GHL Private Integration token (scopes: contacts, conversations/messages, opportunities) | Client | Token + Location ID |
| 4 | `python scripts/ghl_probe.py`: confirms the `Version` header value and token scopes | Us | Prints `Use HGE_GHL_API_VERSION=…` |
| 5 | `python scripts/fieldpulse_probe.py`: prints real payload keys and checks every field the adapter expects | Us | `ALL CHECKS PASSED` (edit `FieldMap` until true) |
| 6 | Set the **status vocab** in `FieldMap` (what "completed", "cancelled", "approved", "paid" look like in this account) | Us | Probe output matches |
| 7 | Configure capacity: `HGE_FP_TECH_COUNT`, `HGE_FP_SLOTS_PER_TECH_PER_DAY`, `HGE_FP_WORKDAYS` | Client + us | Matches how dispatch really books |
| 8 | Create GHL pipeline + stages; put IDs in `HGE_GHL_PIPELINE_ID` / `HGE_GHL_STAGE_IDS` (else stages become `stage:<name>` tags) | Us | Contact moves in pipeline |
| 9 | Build the two GHL tag workflows: `trigger:rvm` → ringless voicemail; `trigger:imessage` → Sendblue webhook | Us | Test contact receives each |
| 10 | Point GHL/Make at the engine webhooks (section 3) | Us | Test lead produces SMS + email |
| 11 | Counsel sign-off on `docs/security-compliance.md` open items | Client | Written approval |
| 12 | Deploy with `HGE_ENV=prod`, `HGE_MODE=live`, **`HGE_CAMPAIGNS_LIVE=false`** | Us | `/readyz` is 200 |
| 13 | Run `POST /admin/jobs/fill-calendar?dry_run=true` for a week; compare *slow days* to what dispatch says | Client + us | Plans match reality |
| 14 | First live send: set `HGE_FILL_MAX_TARGETS=10`; review the actual recipients and replies | Client | No complaints, bookings arrive |
| 15 | Raise the cap gradually; set `HGE_CAMPAIGNS_LIVE=true` | Us | Stable for a week |

## 2. Configuration

All variables are in `.env.example` (prefix `HGE_`). Production refuses placeholder secrets; live mode refuses to boot
without GHL and FieldPulse credentials.

## 3. Wiring the webhooks

| Event | Engine endpoint | Auth |
|---|---|---|
| Website / Meta / LSA lead | `POST /webhooks/leads/{website\|meta\|lsa}` | HMAC headers `X-Timestamp`, `X-Signature: sha256=…`; optional `X-Event-Id` |
| GHL inbound SMS | `POST /webhooks/ghl/inbound` body `{"contact_id","message"}` | HMAC or static token |
| GHL missed call | `POST /webhooks/ghl/missed-call` body `{"phone","name"}` | HMAC or static token |
| FieldPulse job status | `POST /webhooks/fieldpulse/<HGE_FIELDPULSE_WEBHOOK_TOKEN>` | secret path |

Lead payload: `{"name","phone","email","issue","sms_consent"}` (`sms_consent` must come from a real form checkbox).
Sign test requests with `scripts/sign_webhook.py`. For Make/n8n compute `sha256=HMAC(secret, timestamp + "." + body)`.

## 4. Operations

| Task | How |
|---|---|
| Health | `GET /healthz` (liveness), `GET /readyz` (DB reachable), `GET /metrics` |
| Plan a campaign without sending | `POST /admin/jobs/fill-calendar?dry_run=true` (also `estimates`, `database`) |
| Force a run past a guard | add `&force=true` (audited) |
| Reconcile with FieldPulse now | `POST /admin/jobs/sync` |
| See what happened | `GET /admin/audit?limit=100` |
| Backup | copy `/data/engine.db` (WAL-safe with `sqlite3 .backup`) |

**Metrics to alert on:** `hge_send_failures_total` rising · `hge_runner_errors_total` > 0 · `hge_sync_errors_total` > 0 ·
`hge_webhook_rejected_total` spike (probe or misconfigured sender) · `hge_campaign_blocked_total` (anomaly guard fired:
check FieldPulse before forcing) · `hge_booking_failures_total` (FieldPulse write failing: customers are being told a
human will follow up).

## 5. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Fill campaign reports `anomaly` | FieldPulse returned few/no jobs, or capacity set too high | Run `fieldpulse_probe.py`; check key, status vocab and capacity; then re-run |
| Everyone shows as `lead` forever | `FieldMap` status vocab doesn't match account | Probe output → edit `completed_statuses` etc. |
| No SMS goes out but email does | Contact lacks `sms_consent` (by design) or A2P not approved | Check `step_skipped` rows in audit |
| Messages arrive at 8:00 AM | Quiet hours deferral (by design) | Adjust `HGE_QUIET_HOURS_*` with counsel's approval |
| Webhook 401s | Wrong secret, clock skew > 5 min, or body altered by a proxy | Re-sign; check NTP; sign the exact bytes sent |
| FieldPulse 429s | >50 req/s | Client backs off automatically; raise `HGE_FP_SNAPSHOT_TTL_SECONDS` |
| Booking says "a team member will text you" | FieldPulse write failed | Check `booking_failures_total`, FieldPulse status, the `needs-human` tag in GHL |

## Known unverified items

These could not be confirmed without live credentials. Each has a probe or a single place to adjust.

1. **FieldPulse field names**: job `start_time`/`status`/`customer_id`, customer `first_name`/`phone`/`sms_opt_in`, etc. The
   public docs I could read don't list them. → `FieldMap` + `fieldpulse_probe.py`.
2. **Creating a lead / job via POST** body shapes in `FieldPulseClient.create_lead/create_job`. → verify in the Postman collection.
3. **GHL `Version` header.** Default `2021-07-28`; one docs page showed `v3`. → `ghl_probe.py` tries both.
4. **GHL send-message body** for SMS/Email and the opportunities upsert. Implemented from the public reference; the probe sends
   one real test SMS to a number you choose.
5. **Date filtering in FieldPulse `/jobs`.** Documented `filter` syntax wasn't available, so filtering is client-side; fine
   for a few thousand jobs, revisit beyond that (incremental sync on `updated_at`).
6. **Capacity model** (ADR 0003) vs how dispatch actually books.
7. **`fill_expected_booking_rate=0.08`** is an assumption. Measure real conversion in the first weeks and update it.

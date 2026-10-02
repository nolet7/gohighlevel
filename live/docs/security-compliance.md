# Security and compliance

> Not legal advice. Messaging rules (TCPA, CTIA, carrier A2P 10DLC, state mini-TCPA laws) vary by state and use case.
> Items marked **COUNSEL** need sign-off from the client's attorney before go-live.

## Threat model summary

| Threat | Control | Test |
|---|---|---|
| Forged lead / inbound / missed-call webhooks | HMAC-SHA256 over `timestamp.body`, constant-time compare | `test_wrong_secret_rejected`, `test_tampered_body_rejected` |
| Replay of a captured webhook | 5-minute timestamp window + per-event idempotency | `test_stale_timestamp_rejected_replay_protection`, `test_duplicate_event_id_processed_once` |
| Forged FieldPulse "paid" event | Secret path token, payload ignored, state re-read from API | `test_fieldpulse_webhook_is_a_hint_and_reconciles_from_source` |
| Oversized / malformed bodies | 64 KB cap, strict pydantic models, garbage-tolerant FieldPulse route | `test_oversized_payload_rejected`, `test_fieldpulse_webhook_tolerates_garbage` |
| Unauthorized admin actions | Bearer key (constant-time), every admin job audited | `test_admin_requires_bearer_token` |
| Weak secrets in production | `HGE_ENV=prod` refuses placeholders | `test_prod_rejects_placeholder_secrets` |
| PII leakage | Logs mask phones; metrics are counters only; dry-run plans carry first name + ids | `test_health_ready_metrics`, `test_dry_run_plans_but_changes_nothing` |
| Runaway sends from bad data | Anomaly guard, dry-run, hard target cap, weekly cap, run window | `test_empty_looking_calendar_is_treated_as_bad_data…` |
| Container compromise | Non-root user, read-only FS, all caps dropped, no-new-privileges | `docker-compose.yml` |

**Static-token mode.** GHL's native webhook action sends static headers only. Set `HGE_WEBHOOK_STATIC_TOKEN` to accept
`X-Webhook-Token` on webhook routes. This forfeits replay protection, so prefer routing GHL events through Make/n8n
(which can sign) where possible, and keep the token out of GHL workflow screenshots and exports.

**Secrets.** Environment variables only; never committed (`.gitignore`), never logged. Rotate `HGE_WEBHOOK_SECRET` by
updating the engine and the signing relay in one maintenance window (a few seconds of 401s is expected).

## Messaging compliance behaviors (implemented)

| Rule | Behavior |
|---|---|
| Consent | Text channels (SMS/RVM/iMessage) send only if `sms_consent` is true: set from the form checkbox, FieldPulse marketing opt-in, or the contact texting first. Otherwise the step is *skipped* and email still goes. |
| Opt-out | Exact-keyword match (`STOP, STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT`) sets DND, cancels **every** sequence, mirrors DND to GHL, sends one confirmation. "Please stop by tomorrow" is *not* an opt-out. |
| Opt-in / help | `START`/`UNSTOP` re-subscribes; `HELP` returns contact and opt-out instructions. |
| Quiet hours | No sequence sends 21:00–08:00 local (configurable). Steps are deferred, not dropped, and **delays restart from the actual send** so nothing bursts at 8 AM. Direct replies to an inbound text are exempt. |
| Frequency cap | Max 4 marketing messages / contact / rolling 7 days (configurable). Speed-to-lead and review requests don't count. |
| Identification | Opening SMS in each marketing sequence includes the business name and "Reply STOP to opt out". |
| Safety | Gas / carbon-monoxide language never triggers auto-booking: sends a 911 / leave-the-building message and tags `needs-human`. |

### Decisions that need COUNSEL / the client

1. **Speed-to-lead during quiet hours.** A 10:30 PM web lead currently gets nothing until 8 AM. Many shops prefer an
   instant reply because the lead just asked for contact. Allowed or not varies by state; if approved, add a
   quiet-hours bypass for the first step only.
2. **Missed-call text-back as implied consent** (`HGE_MISSED_CALL_IMPLIED_CONSENT=true`). Defensible as a reply to the
   caller's own inquiry, but confirm.
3. **Past-customer consent.** The engine trusts FieldPulse's marketing opt-in flag. Confirm how, and whether, existing
   customers consented to marketing texts; reactivating a database without documented consent is the highest-risk item
   in this project.
4. **Record retention** for the message log and audit trail (currently kept indefinitely).
5. **Carrier registration.** A2P 10DLC brand + campaign registration must be approved *before* any production traffic.

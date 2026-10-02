# ADR 0004: FieldPulse webhooks are hints; truth is re-read

**Status:** accepted

**Context.** FieldPulse offers job-status webhooks but documents no signing scheme, so authenticity can't be proven.
Our own inbound webhooks (leads, GHL events) are different: we control their senders and sign them.

**Decision.** `POST /webhooks/fieldpulse/{secret-token}`: the token (constant-time compared) gates the route, but
the body is never trusted. We read at most a customer id from it, force-refresh the FieldPulse snapshot and
reconcile from the API. A forged "paid" event therefore changes nothing (tested).

**Consequence.** One extra API read per event. Acceptable at the 50 req/s limit. The periodic `sync_all` tick
remains the safety net for missed or dropped webhooks.

# Team demo script (≈ 15 minutes)

**Setup (once):** `pip install -e ".[dev]" && make demo` prints everything below with PASS/FAIL checks. For the live-API
part: `cp .env.example .env`, then `make run` in one terminal.

**Framing (30 s).** "FieldPulse stays our system of record. GHL does the messaging. This service decides who to
contact, when it's allowed, and when to stop. Everything you'll see runs against a simulated FieldPulse and GHL; nothing
is sent to real people."

| # | Say / show | Proof on screen | Point to land |
|---|---|---|---|
| 1 | Three leads arrive: website, Meta, Google LSA. A fourth ticked no text consent. | Maria gets SMS + email instantly. Eli (no consent) gets **email only**. | Speed-to-lead *and* consent are enforced in code. |
| 2 | Someone calls and nobody answers. | Text-back arrives, includes "Reply STOP". | Missed-call recovery. |
| 3 | Dana: "no heat at all". Tom: "what does a tune-up cost?". Then Tom: "I smell gas". | Dana is booked into FieldPulse same-day, in the future. Tom is asked a question, then gets the **911 message** and a `needs-human` tag, with **no** booking. | AI books, but never for safety events. |
| 4 | Maria doesn't answer: fast-forward 5 h and 25 h. Then she replies "yes book me". | Nudges at +4 h, +24 h. After booking, 8 days pass: **nothing more is sent**. | Follow-ups stop the moment she books. |
| 5 | Office approves an estimate, completes the job, records payment in FieldPulse. | Stage walks Booked → Won → Job Complete → Paid; review request is sent. | FieldPulse is the source of truth; GHL mirrors it. |
| 6 | Ken's estimate went unsold 9 days. | Reactivation text; when Ken approves in FieldPulse the sequence stops. | Unsold-estimate recovery. |
| 7 | **The headline.** Show the 8-day calendar: two days are mostly empty. Run the **dry run** first. | "Would message 40 of 63 candidates". Then run it live: one SMS naming *Tue Oct 20, Fri Oct 23*, the offer, and STOP language. | Slow days are detected from FieldPulse and the campaign is sized to the open capacity; dry-run before any real send. |
| 8 | Run it again. Then a customer replies YES; another replies STOP. | Second run is a no-op (run guard). YES books them; STOP sets DND everywhere. | Safe to schedule daily. |
| 9 | A lead arrives at 10:30 PM. | Nothing sent. At 8:05 AM it goes out, and the +4 h nudge comes at 12:05, not in a burst. | Quiet hours without losing the lead. |

**Optional live-API segment (3 min).**

    export HGE_WEBHOOK_SECRET=change-me
    python scripts/sign_webhook.py http://localhost:8000/webhooks/leads/website \
        '{"name":"Demo Lead","phone":"404-555-0111","email":"demo@example.com","sms_consent":true}'   # 200 ok
    # run the exact same command again          -> {"status":"duplicate"}
    curl -X POST localhost:8000/webhooks/leads/website -d '{}'                                         # 401
    curl -X POST "localhost:8000/admin/jobs/fill-calendar?dry_run=true" -H "Authorization: Bearer change-me-too"
    curl localhost:8000/metrics

**Likely questions.**

- *Is this connected to our real FieldPulse yet?* No. FieldPulse issues API keys on request; the probe script turns
  discovery into a ten-minute task (runbook §1). 
- *Can it see our real schedule?* FieldPulse has no availability endpoint, so open slots are derived from jobs and a
  configured capacity (ADR 0003). That's the main accuracy lever to tune together.
- *What stops it texting someone it shouldn't?* Consent flag, DND, quiet hours, weekly cap, anomaly guard, and
  dry-run-by-default scheduling. Each has a test.
- *How do we onboard the next client on ServiceTitan?* New `FieldServicePort` adapter; services and tests carry over (ADR 0001).

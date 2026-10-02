# HVAC Growth Engine

Lead follow-up, AI booking and **calendar-fill campaigns** for home-service companies, running alongside
**FieldPulse** (system of record) and **GoHighLevel** (messaging, pipeline, AI conversations).

| | |
|---|---|
| Tests | 139 passing, 95% coverage (CI gate: 90%) |
| Lint | ruff clean |
| Runtime | Python 3.11+, FastAPI, SQLite (swap-ready), Docker |
| Modes | `mock` (demo / tests, no credentials) and `live` (GHL + FieldPulse) |

## What it does

| Capability | Where |
|---|---|
| Website / Meta Lead Ad / Google LSA lead → instant SMS + email | `services/intake.py`, `POST /webhooks/leads/{website,meta,lsa}` |
| Missed-call text-back | `services/conversation.py`, `POST /webhooks/ghl/missed-call` |
| AI qualification + booking into FieldPulse (safety escalation for gas/CO) | `services/conversation.py` |
| Follow-up sequences for non-bookers | `sequences.py`, `services/runner.py` |
| FieldPulse → GHL status sync; **automations stop on booked / converted / completed / paid** | `services/sync.py` |
| **Fill-slow-days campaign** from FieldPulse capacity 7–14 days out | `services/campaigns.py` |
| Unsold-estimate and past-customer reactivation | `services/campaigns.py` |
| Review request after payment | `sequences.py` |
| Ringless voicemail / Sendblue (iMessage) | tag-triggered GHL workflows (`adapters/ghl.py`) |
| Compliance: consent, STOP/START/HELP, quiet hours, weekly cap, DND | `compliance.py` |

## Going live

Step-by-step: [docs/implementation-guide.md](docs/implementation-guide.md).

## Quick start

    pip install -e ".[dev]"
    make test            # 139 tests
    make demo            # 9-step walkthrough, 22 checks (see docs/demo-script.md)
    cp .env.example .env && make run      # API on :8000, mock mode

    docker compose up --build

## Safety properties (all covered by tests)

- **Dry-run everywhere**: every campaign can plan without sending; scheduled campaigns only *plan* until `HGE_CAMPAIGNS_LIVE=true`.
- **Anomaly guard**: if >75% of workdays look empty (API outage / wrong capacity config), the fill campaign refuses to run.
- **Run guards**: one fill campaign per slow-day set per day; jobs fire only inside a 3-hour window (no late blast after a restart).
- **Signed webhooks** (HMAC-SHA256 + 5-minute replay window) and idempotent event handling.
- **FieldPulse webhooks are hints, not truth**: payload is ignored and state is re-read from the API.
- **No burst after deferral**: step delays are measured from the previous send, not from enrollment.
- Prod refuses to boot with placeholder secrets; logs and metrics never contain phone numbers or message text.

## Layout

    src/hvac_engine/
      domain.py ports.py config.py store.py compliance.py sequences.py
      services/   intake · runner · conversation · sync · campaigns
      adapters/   mock (tests/demo) · ghl · fieldpulse · http (retry/backoff)
      api.py security.py scheduler.py container.py demo.py
    tests/        139 tests: unit, contract (mocked HTTP), API, end-to-end demo
    scripts/      fieldpulse_probe.py · ghl_probe.py · sign_webhook.py
    docs/         implementation-guide · architecture · runbook · security-compliance · demo-script · adr/

## Status: read before go-live

Built and tested against **mocks and recorded API shapes**. Not yet run against a live GHL sub-account or
FieldPulse account. Specific unverified items are tracked in [docs/runbook.md](docs/runbook.md#known-unverified-items):
FieldPulse field names, GHL `Version` header value, stage/pipeline IDs, A2P 10DLC registration.

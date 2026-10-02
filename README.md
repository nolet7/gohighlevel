# gohighlevel

GoHighLevel + FieldPulse automation for an HVAC company: instant lead follow-up, AI booking, missed-call
text-back, FieldPulse status sync, and campaigns that fill slow days on the technician calendar.

| Folder | What it is |
|---|---|
| [`live/`](live/) | **Production service.** Packaged FastAPI app, 139 tests (95% coverage), Docker, CI, runbook. Start at [`live/README.md`](live/README.md). |
| [`proto/`](proto/) | The original single-file prototype used for the first team demo. Kept for reference; superseded by `live/`. |

## Taking `live/` to production

Follow [`live/docs/implementation-guide.md`](live/docs/implementation-guide.md): eight phases, each with an exit check.
Background: [runbook](live/docs/runbook.md) · [architecture](live/docs/architecture.md) ·
[security & compliance](live/docs/security-compliance.md) · [team demo script](live/docs/demo-script.md).

## Status

Built and tested against mocks and recorded API shapes; **not yet run against a live GHL or FieldPulse account**.
Open items are listed in the runbook under "Known unverified items".

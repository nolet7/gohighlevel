"""Team demo against the mock adapters: `hge-demo` or `python -m hvac_engine.demo`."""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .adapters.mock import MockCRM, MockFieldPulse
from .clock import FakeClock
from .config import Settings
from .container import build_engine
from .domain import LeadIn
from .store import Store

TZ = ZoneInfo("America/New_York")


def main() -> int:
    clock = FakeClock(datetime(2026, 10, 2, 14, 40, tzinfo=TZ))
    s = Settings(database_path=":memory:")
    crm, fs = MockCRM(), MockFieldPulse(clock, s.timezone)
    e = build_engine(s, crm=crm, fs=fs, clock=clock, store=Store(":memory:"))
    results: list[bool] = []

    def step(n: int, title: str) -> None:
        print(f"\n{'=' * 74}\nSTEP {n}: {title}\n{'=' * 74}")

    def check(label: str, ok: bool) -> None:
        results.append(bool(ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")

    def msgs(cid: str) -> list[dict]:
        return [m for m in crm.sent if m["cid"] == cid]

    def show(ms: list[dict]) -> None:
        for m in ms:
            print(f"   {m['channel'].upper():5} -> {m['text']}")

    def local_today() -> date:
        return clock.now().astimezone(TZ).date()

    # ---- seed FieldPulse with past customers, one opted out, one unsubscribed
    for i in range(60):
        fs.add_customer(f"Customer{i} Smith", f"+1404555{1000 + i}", f"c{i}@example.com",
                        last_service=date(2025, 9, 1) - timedelta(days=i * 20), system="HVAC")
    fs.add_customer("Nina NoConsent", "+14045558888", "nina@example.com", date(2024, 5, 1), opt_in=False)
    pat = fs.add_customer("Pat OptOut", "+14045559999", "pat@example.com", date(2024, 1, 5))
    pat_cid, _ = crm.upsert_contact("Pat OptOut", "+14045559999", "pat@example.com", set())
    from .domain import ContactRecord
    e.store.save_contact(ContactRecord(pat_cid, "Pat OptOut", "+14045559999", "pat@example.com", pat, dnd=True,
                                       sms_consent=True), clock.now())
    for off in range(0, 16):
        fs.seed_load(local_today() + timedelta(days=off), 11)

    step(1, "Website / Meta / LSA leads: instant follow-up, synced to FieldPulse")
    maria = e.intake.ingest(LeadIn("website", "Maria Lopez", "(404) 555-0101", "maria@example.com", "AC not cooling", True))
    tom = e.intake.ingest(LeadIn("meta_lead_ad", "Tom Reed", "404-555-0102", "tom@example.com", "furnace tune-up", True))
    dana = e.intake.ingest(LeadIn("google_lsa", "Dana Cruz", "+14045550103", "dana@example.com", "no heat", True))
    eli = e.intake.ingest(LeadIn("website", "Eli NoText", "+14045550105", "eli@example.com", "AC check", False))
    show(msgs(maria))
    check("Maria got SMS + email immediately", {m["channel"] for m in msgs(maria)} == {"sms", "email"})
    check("Lead without text consent got email only (compliance)", [m["channel"] for m in msgs(eli)] == ["email"])

    step(2, "Missed-call text-back")
    mc = e.conversation.missed_call("+14045550104")
    show(msgs(mc))
    check("Text-back sent", len(msgs(mc)) == 1)

    step(3, "AI qualification and booking against FieldPulse availability")
    e.conversation.handle_inbound(dana, "My furnace is not working, no heat at all")
    e.conversation.handle_inbound(tom, "what does a tune-up cost?")
    show(msgs(dana)[-1:])
    show(msgs(tom)[-1:])
    check("Urgent lead booked", crm.stages[dana] == "Booked")
    check("Non-urgent lead asked a question", crm.stages[tom] == "New Lead")
    e.conversation.handle_inbound(tom, "I smell gas near the furnace")
    check("Gas smell -> safety message + human escalation", "needs-human" in crm.tags[tom])

    step(4, "Nudges for non-bookers; stop the moment they book")
    before = len(msgs(maria))
    clock.advance(hours=5); e.tick()
    clock.advance(hours=20); e.tick()
    check("Maria received +4h and +24h nudges", len(msgs(maria)) - before == 2)
    e.conversation.handle_inbound(maria, "Yes please book me")
    n = len(msgs(maria)); clock.advance(days=8); e.tick()
    check("No more follow-ups after booking", len(msgs(maria)) == n)

    step(5, "FieldPulse sync: booked -> converted -> completed -> paid")
    mfp = e.store.get_contact(maria).fp_id
    fs.add_estimate(mfp, 7200, clock.now(), "approved")
    e.sync.sync_contact(maria); print("  stage:", crm.stages[maria])
    fs.complete_jobs(mfp); e.sync.sync_contact(maria); print("  stage:", crm.stages[maria])
    fs.mark_paid(mfp); e.sync.sync_contact(maria); print("  stage:", crm.stages[maria])
    clock.advance(hours=3); e.tick()
    check("Reached Paid stage", crm.stages[maria] == "Paid / Review Request")
    check("Review request sent after payment", "review" in msgs(maria)[-1]["text"].lower())

    step(6, "Unsold estimate reactivation")
    ken = fs.add_customer("Ken Ford", "+14045550201", "ken@example.com", date(2026, 3, 1))
    ken_est = fs.add_estimate(ken, 9800, clock.now() - timedelta(days=9))
    plan = e.campaigns.reactivate_estimates()
    check("Ken enrolled", plan.enrolled == 1)
    fs.set_estimate_status(ken_est, "approved"); e.sync.sync_all()
    kc = e.store.contact_by_fp(ken).id
    check("Stops when Ken approves in FieldPulse", not e.store.is_enrolled(kc))

    step(7, "Calendar-fill: read FieldPulse capacity 7-14 days out, target past customers")
    today = local_today()
    fs.clear_days(today + timedelta(days=7), today + timedelta(days=14))
    open_days = [today + timedelta(days=o) for o in range(7, 15) if fs.capacity(today + timedelta(days=o))]
    slow = [open_days[1], open_days[4]]
    for d in open_days:
        fs.seed_load(d, 3 if d in slow else 11)
    for d, a in e.deps.fs.availability(today + timedelta(days=7), today + timedelta(days=14)).items():
        print(f"   {d:%a %b %d}: {a.open:2d}/{a.capacity:2d} open" + ("   <-- SLOW" if d in slow else ""))
    dry = e.campaigns.fill_calendar(dry_run=True)
    print(f"  DRY RUN: would message {dry.enrolled} of {dry.candidates} candidates (budget {dry.budget}); skipped {dry.skipped}")
    check("Dry run sends nothing", not any("openings on" in m["text"] for m in crm.sent))
    plan = e.campaigns.fill_calendar()
    sample = next(m for m in crm.sent if "openings on" in m["text"])
    show([sample])
    check("Detected exactly the 2 slow days", set(plan.slow_days) == {d.isoformat() for d in slow})
    check("Sized to open capacity, not the whole database", 0 < plan.enrolled <= plan.budget < plan.candidates)
    check("Opted-out / no-consent customers never targeted",
          plan.skipped.get("no_marketing_opt_in", 0) >= 1 and plan.skipped.get("dnd", 0) >= 1)
    check("Second run same day is a no-op (run guard)", "already ran" in e.campaigns.fill_calendar().note)

    step(8, "Recipients who reply YES get booked; STOP honoured; quiet hours respected")
    recips = [m["cid"] for m in crm.sent if "openings on" in m["text"]]
    yes_cid, stop_cid = recips[0], recips[1]
    e.conversation.handle_inbound(yes_cid, "YES book me")
    check("Campaign recipient who replies YES is booked in FieldPulse", crm.stages[yes_cid] == "Booked")
    e.conversation.handle_inbound(stop_cid, "STOP")
    check("STOP sets DND on the recipient", e.store.get_contact(stop_cid).dnd and crm.dnd[stop_cid])
    clock.set(datetime(2026, 10, 20, 22, 30, tzinfo=TZ))                      # 10:30 PM local
    before = len(crm.sent)
    late = e.intake.ingest(LeadIn("website", "Late Night", "+14045550777", "late@example.com", "AC", True))
    check("10:30 PM lead: nothing sent during quiet hours", len(crm.sent) == before)
    clock.set(datetime(2026, 10, 21, 8, 5, tzinfo=TZ)); e.tick()              # 8:05 AM next day
    check("Deferred follow-up goes out at 8:05 AM", len(msgs(late)) >= 1)

    step(9, "Database reactivation (dry run first, as in production rollout)")
    p = e.campaigns.reactivate_database(dry_run=True)
    print(f"  DRY RUN: {p.enrolled} would be enrolled of {p.candidates} candidates")
    check("Database plan produced", p.candidates > 0)

    print(f"\n{'=' * 74}\nRESULT: {sum(results)}/{len(results)} checks passed\n{'=' * 74}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

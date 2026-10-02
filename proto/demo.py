"""Team demo: python demo.py   (runs offline against mock GHL + FieldPulse)"""
from datetime import datetime, date, timedelta
from adapters import Clock, MockFieldPulse, MockGHL
from engine import Engine

clock = Clock(datetime(2026, 10, 2, 14, 40))
fp, ghl = MockFieldPulse(clock), MockGHL(clock)
eng = Engine(ghl, fp, clock)
checks = []


def step(n, title):
    print(f"\n{'=' * 72}\nSTEP {n}: {title}\n{'=' * 72}")


def check(label, cond):
    checks.append(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def msgs(gid, since=0):
    return [m for m in ghl.messages if m["cid"] == gid][since:]


def show(ms):
    for m in ms:
        arrow = "->" if m["dir"] == "out" else "<-"
        print(f"   {m['at']:%a %H:%M} {m['channel'].upper():5} {arrow} {m['text']}")


# ---- seed FieldPulse with realistic data
for i in range(40):
    fp.add_customer(f"Customer{i} Smith", f"+1404555{1000 + i}", f"c{i}@example.com",
                    last_service=date(2025, 9, 1) - timedelta(days=i * 20),
                    system="AC" if i % 2 else "furnace")
opt_out = fp.add_customer("Pat OptOut", "+14045559999", "pat@example.com", date(2024, 1, 5))
today = clock.now.date()
for off in range(1, 16):                     # busy calendar...
    fp.seed_load(today + timedelta(days=off), 11)
slow_days = [today + timedelta(days=9), today + timedelta(days=11)]
for d in slow_days:                          # ...except two slow days
    fp.jobs = [j for j in fp.jobs if j["start"].date() != d]
    fp.seed_load(d, 3)
ghl.upsert_contact("Pat OptOut", "+14045559999", "pat@example.com", opt_out)
ghl.contacts["ghl1"]["dnd"] = True           # Pat previously said STOP

# ------------------------------------------------------------------ 1
step(1, "Website / Meta / LSA lead -> instant SMS + email, synced to FieldPulse")
g_web = eng.ingest_lead("website", "Maria Lopez", "+14045550101", "maria@example.com", "AC not cooling")
g_meta = eng.ingest_lead("meta_lead_ad", "Tom Reed", "+14045550102", "tom@example.com", "furnace tune-up")
g_lsa = eng.ingest_lead("google_lsa", "Dana Cruz", "+14045550103", "dana@example.com", "no heat")
show(msgs(g_web))
check("Maria got SMS + email in the same minute", {m["channel"] for m in msgs(g_web)} == {"sms", "email"})
check("All 3 sources created FieldPulse leads", all(ghl.contacts[g]["fp_id"] for g in (g_web, g_meta, g_lsa)))

# ------------------------------------------------------------------ 2
step(2, "Missed-call text-back")
g_mc = eng.missed_call("+14045550104")
show(msgs(g_mc))
check("Text-back sent to missed caller", len(msgs(g_mc)) == 1)

# ------------------------------------------------------------------ 3
step(3, "AI qualification + booking against live FieldPulse availability")
eng.handle_inbound(g_lsa, "My furnace is not working, no heat at all")
eng.handle_inbound(g_meta, "what does a tune-up cost?")
show(msgs(g_lsa)[-2:])
show(msgs(g_meta)[-2:])
check("Urgent lead booked into FieldPulse", any(j["customer_id"] == ghl.contacts[g_lsa]["fp_id"] for j in fp.jobs))
check("Non-urgent lead asked a qualifying question, not booked",
      not any(j["customer_id"] == ghl.contacts[g_meta]["fp_id"] for j in fp.jobs))

# ------------------------------------------------------------------ 4
step(4, "Follow-up sequence for leads that do not book, then stop on booking")
before = len(msgs(g_web))
clock.advance(hours=5); eng.run_sequences()
clock.advance(hours=20); eng.run_sequences()
show(msgs(g_web)[before:])
check("Maria received +4h and +24h nudges", len(msgs(g_web)) - before == 2)
eng.handle_inbound(g_web, "Yes please book me")      # she books
n = len(msgs(g_web))
clock.advance(days=8); eng.run_sequences()
check("No further follow-ups after Maria booked", len(msgs(g_web)) == n)
check("GHL stage = Booked", ghl.opps[g_web] == "Booked")

# ------------------------------------------------------------------ 5
step(5, "FieldPulse status sync: booked -> converted -> completed -> paid")
fp_id = ghl.contacts[g_web]["fp_id"]
est = fp.create_estimate(fp_id, 7200, clock.now); fp.estimates[est]["status"] = "approved"
eng.sync_from_fieldpulse(); print("  stage:", ghl.opps[g_web])
for j in fp.jobs:
    if j["customer_id"] == fp_id: j["status"] = "completed"
eng.sync_from_fieldpulse(); print("  stage:", ghl.opps[g_web])
fp.invoices["inv1"] = dict(customer_id=fp_id, paid=True)
eng.sync_from_fieldpulse(); print("  stage:", ghl.opps[g_web])
check("Reached Paid stage", ghl.opps[g_web] == "Paid / Review Request")
check("Review request sent on payment", "review" in msgs(g_web)[-1]["text"].lower())

# ------------------------------------------------------------------ 6
step(6, "Unsold estimate reactivation")
cid = fp.add_customer("Ken Ford", "+14045550201", "ken@example.com", date(2026, 3, 1))
fp.create_estimate(cid, 9800, clock.now - timedelta(days=9))
eng.reactivate_estimates()
gk = [g for g, c in ghl.contacts.items() if c["fp_id"] == cid][0]
show(msgs(gk))
check("Ken (unsold 9 days) got reactivation text", len(msgs(gk)) == 1)
fp.estimates[next(iter(e for e in fp.estimates if fp.estimates[e]["customer_id"] == cid))]["status"] = "approved"
eng.sync_from_fieldpulse()
n = len(msgs(gk)); clock.advance(days=10); eng.run_sequences()
check("Reactivation stops once Ken approves in FieldPulse", len(msgs(gk)) == n)

# ------------------------------------------------------------------ 7
step(7, "Fill-the-calendar campaign from FieldPulse availability (7-14 days out)")
today = clock.now.date()                      # clock has advanced since setup: re-seed the window
fp.jobs = [j for j in fp.jobs if not (today + timedelta(days=7) <= j["start"].date() <= today + timedelta(days=14))]
open_days = [today + timedelta(days=o) for o in range(7, 15) if fp.capacity(today + timedelta(days=o))]
slow_days = [open_days[1], open_days[4]]      # two workdays that are mostly empty
for d in open_days:
    fp.seed_load(d, 3 if d in slow_days else 11)
avail =fp.availability(today + timedelta(days=7), today + timedelta(days=14))
for d, n_ in avail.items():
    print(f"   {d:%a %b %d}: {n_:2d}/{fp.capacity(d):2d} slots open" + ("   <-- SLOW" if d in slow_days else ""))
res = eng.fill_calendar()
sample = next(m for m in ghl.messages if "openings on" in m["text"])
print("  sample message:"); show([sample])
check("Detected exactly the 2 slow days", set(res["slow"]) == set(slow_days))
check("Campaign sized to open capacity (not the whole database)", 0 < res["enrolled"] <= 20)
check("Opted-out customer never texted", not any(m["cid"] == "ghl1" for m in ghl.messages))
check("Oldest-service customers targeted first", ghl.contacts[ghl.messages[-1]["cid"]]["fp_id"] is not None)

# ------------------------------------------------------------------ 8
step(8, "Campaign stops for anyone who books; STOP keyword honored")
tgt = next(e["cid"] for e in ghl.enrollments if e["seq"] == "fill_calendar")
eng.handle_inbound(tgt, "YES book me")
check("Campaign enrollment cancelled after booking", not ghl.is_enrolled(tgt, "fill_calendar"))
tgt2 = next(e["cid"] for e in ghl.enrollments if e["seq"] == "fill_calendar" and e["cid"] != tgt)
eng.handle_inbound(tgt2, "STOP")
check("STOP sets DND and halts sequences", ghl.contacts[tgt2]["dnd"] and not ghl.is_enrolled(tgt2))

# ------------------------------------------------------------------ 9
step(9, "Past-customer database reactivation")
print("  enrolled:", eng.reactivate_database())

print("\n" + "=" * 72)
print(f"RESULT: {sum(checks)}/{len(checks)} checks passed")
print("=" * 72)
raise SystemExit(0 if all(checks) else 1)

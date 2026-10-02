"""Automation engine: the logic that would live in GHL workflows + a small middleware
(n8n / Make / Cloud Function) between GHL and FieldPulse."""
import re
from datetime import timedelta, datetime

SEQUENCES = {
    # name: [(delay_hours_from_enroll, channel, template)]
    "speed_to_lead": [
        (0,   "sms",   "Hi {first}, thanks for contacting {co}! I'm the virtual assistant. What's going on with your {issue}? Reply here and I can get you booked."),
        (0,   "email", "Thanks for reaching out to {co}, {first}. We got your request and will help you right away."),
        (4,   "sms",   "{first}, still need help? We have openings this week. Reply YES and I'll find a time."),
        (24,  "sms",   "Hi {first}, {co} here. Want me to hold a tech slot for you? Just reply with a day that works."),
        (72,  "email", "{first}, a quick note from {co}: we can usually get a tech out within 48 hours. Reply to book."),
        (168, "sms",   "Last check-in from {co}, {first}. Reply BOOK anytime and we'll get you on the schedule."),
    ],
    "estimate_reactivation": [
        (0,   "sms",   "Hi {first}, {co} here. Still thinking about the estimate we sent? Happy to answer questions or adjust the scope."),
        (72,  "email", "{first}, our estimate is still available. Financing options can bring the monthly cost down. Reply to chat."),
        (168, "sms",   "{first}, this week we can add $50 off your install if you approve. Interested?"),
    ],
    "db_reactivation": [
        (0,   "sms",   "Hi {first}, it's {co}. It's been a while since your last service. Want a tune-up booked? Reply YES."),
        (96,  "email", "{first}, regular maintenance prevents breakdowns. Reply to schedule your {system} tune-up with {co}."),
    ],
    "fill_calendar": [
        (0,   "sms",   "Hi {first}, {co} has openings on {days}. Book your {season} tune-up this week for {offer}. Reply YES to claim a slot."),
        (0,   "email", "{first}, we have a few open slots on {days}. {offer} on {season} tune-ups. Reply to book."),
    ],
}

# Which GHL sequences stop for each FieldPulse state (the "stop appropriately" requirement)
STOP_RULES = {
    "booked":    {"speed_to_lead", "estimate_reactivation", "db_reactivation", "fill_calendar"},
    "converted": {"speed_to_lead", "estimate_reactivation", "db_reactivation", "fill_calendar"},
    "completed": {"speed_to_lead", "estimate_reactivation", "db_reactivation", "fill_calendar"},
    "paid":      {"speed_to_lead", "estimate_reactivation", "db_reactivation", "fill_calendar"},
}
STAGE_FOR_STATE = {"lead": "New Lead", "booked": "Booked", "converted": "Won - Install",
                   "completed": "Job Complete", "paid": "Paid / Review Request"}

COMPANY = "Summit HVAC"
SEASON_BY_MONTH = {12: "winter heating", 1: "winter heating", 2: "winter heating",
                   3: "spring AC", 4: "spring AC", 5: "spring AC",
                   6: "summer AC", 7: "summer AC", 8: "summer AC",
                   9: "fall furnace", 10: "fall furnace", 11: "fall furnace"}


def first(name): return name.split()[0]


class Engine:
    def __init__(self, ghl, fp, clock):
        self.ghl, self.fp, self.clock = ghl, fp, clock
        self.log = []

    def note(self, msg):
        self.log.append((self.clock.now, msg))

    # ------------------------------------------------------------ lead intake
    def ingest_lead(self, source, name, phone, email, issue="HVAC issue"):
        """Website form / Meta Lead Ad / Google LSA all normalize to this call."""
        existing = self.fp.find_customer(phone=phone, email=email)
        fp_id = existing["id"] if existing else self.fp.create_lead(name, phone, email, source)
        gid, new = self.ghl.upsert_contact(name, phone, email, fp_id, tags={f"source:{source}"})
        self.ghl.set_stage(gid, "New Lead")
        self.ghl.contacts[gid]["issue"] = issue
        self.ghl.enroll(gid, "speed_to_lead")
        self.note(f"[{source}] lead {name} -> GHL {gid} / FP {fp_id} ({'new' if new else 'existing'}), speed_to_lead enrolled")
        self.run_sequences()          # steps with delay 0 fire immediately
        return gid

    # ------------------------------------------------------ missed-call text-back
    def missed_call(self, phone, name="there"):
        gid, _ = self.ghl.upsert_contact(name, phone, None, tags={"source:missed_call"})
        self.ghl.send(gid, "sms", f"Hi, sorry we missed your call! This is {COMPANY}. "
                                  "Text us what's going on and we'll get you scheduled.")
        self.note(f"missed call from {phone} -> text-back sent")
        return gid

    # --------------------------------------------------- AI qualification + booking
    def handle_inbound(self, gid, text):
        """Stand-in for GHL Conversation AI. Production: bot prompt + 'book appointment' action."""
        c = self.ghl.contacts[gid]
        self.ghl.receive(gid, text)
        low = text.lower()
        if re.search(r"\b(stop|unsubscribe|quit)\b", low):
            c["dnd"] = True
            self.ghl.cancel(gid)
            self.note(f"{c['name']} opted out -> DND set, sequences stopped")
            return
        urgent = any(w in low for w in ("no heat", "no cool", "no ac", "not cooling", "not working", "leak", "smell gas", "emergency"))
        c.setdefault("qual", {})["urgent"] = urgent
        wants_book = any(w in low for w in ("yes", "book", "schedule", "appointment", "tomorrow", "today", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"))
        if urgent or wants_book:
            slot = self._next_slot(urgent)
            if slot is None:
                self.ghl.send(gid, "sms", "We're fully booked this week, but I've added you to the priority list.")
                return
            fp_id = c["fp_id"]
            self.fp.create_job(fp_id, slot, "emergency" if urgent else "service")
            self.ghl.send(gid, "sms", f"You're booked, {first(c['name'])}! A tech will arrive {slot:%A %b %d at %I:%M %p}. "
                                      "Reply here if you need to change it.")
            self.ghl.add_tag(gid, "ai-booked")
            self.note(f"AI booked {c['name']} for {slot:%a %b %d %H:%M} (urgent={urgent})")
            self.sync_from_fieldpulse()     # immediately stops follow-ups
        else:
            self.ghl.send(gid, "sms", "Thanks! Is your system heating or cooling, and when did the problem start? "
                                      "I can also get you on the schedule, just say BOOK.")

    def _next_slot(self, urgent):
        today = self.clock.now.date()
        start = today if urgent else today + timedelta(days=1)
        avail = self.fp.availability(start, start + timedelta(days=14))
        for d, n in avail.items():
            if n <= 0:
                continue
            if d == today:                       # same-day: arrival must be in the future
                hour = self.clock.now.hour + 2
                if hour > 17:
                    continue
            else:
                hour = 8 + (self.fp.capacity(d) - n) % 9
            return datetime.combine(d, datetime.min.time()).replace(hour=hour)
        return None

    # --------------------------------------------------------------- sequences
    def run_sequences(self):
        """Cron job (every few minutes in prod): fire any due steps."""
        for e in self.ghl.enrollments:
            if not e["active"]:
                continue
            steps = SEQUENCES[e["seq"]]
            c = self.ghl.contacts[e["cid"]]
            while e["step"] < len(steps):
                delay_h, channel, tpl = steps[e["step"]]
                if self.clock.now < e["start"] + timedelta(hours=delay_h):
                    break
                ctx = e.get("ctx", {})
                text = tpl.format(first=first(c["name"]), co=COMPANY, issue=c.get("issue", "HVAC"),
                                  system=ctx.get("system", "system"), season=ctx.get("season", ""),
                                  offer=ctx.get("offer", ""), days=ctx.get("days", ""))
                self.ghl.send(e["cid"], channel, text)
                e["step"] += 1
            if e["step"] >= len(steps):
                e["active"] = False

    # --------------------------------------------------- FieldPulse -> GHL sync
    def sync_from_fieldpulse(self):
        """Poll (or webhook) FieldPulse; push canonical state to GHL; stop sequences."""
        changes = []
        for gid, c in self.ghl.contacts.items():
            if not c["fp_id"]:
                continue
            state = self.fp.customer_state(c["fp_id"])
            if state != c["fp_state"]:
                c["fp_state"] = state
                self.ghl.set_stage(gid, STAGE_FOR_STATE.get(state, "New Lead"))
                self.ghl.add_tag(gid, f"fp:{state}")
                stopped = self.ghl.cancel(gid, STOP_RULES[state]) if state in STOP_RULES else 0
                if state == "paid":
                    self.ghl.send(gid, "sms", f"Thanks for choosing {COMPANY}, {first(c['name'])}! Mind leaving us a quick review?")
                changes.append((c["name"], state, stopped))
                self.note(f"sync: {c['name']} -> {state}, stage '{STAGE_FOR_STATE.get(state)}', stopped {stopped} sequence(s)")
        return changes

    # ------------------------------------------- unsold estimate reactivation
    def reactivate_estimates(self, min_age_days=5):
        n = 0
        for e in self.fp.estimates.values():
            if e["status"] != "sent" or (self.clock.now - e["sent_at"]).days < min_age_days:
                continue
            cust = self.fp.customers[e["customer_id"]]
            gid, _ = self.ghl.upsert_contact(cust["name"], cust["phone"], cust["email"], cust["id"])
            if self.fp.has_open_work(cust["id"]) or self.ghl.is_enrolled(gid):
                continue
            self.ghl.enroll(gid, "estimate_reactivation")
            n += 1
        self.run_sequences()
        self.note(f"estimate reactivation enrolled {n}")
        return n

    # --------------------------------------------------- database reactivation
    def reactivate_database(self, min_months=11, limit=50):
        n = 0
        for c in self.fp.customers.values():
            if n >= limit:
                break
            if c["status"] != "customer" or not c["last_service"]:
                continue
            if (self.clock.now.date() - c["last_service"]).days < min_months * 30:
                continue
            gid, _ = self.ghl.upsert_contact(c["name"], c["phone"], c["email"], c["id"], tags={"past-customer"})
            if self.ghl.contacts[gid]["dnd"] or self.ghl.is_enrolled(gid) or self.fp.has_open_work(c["id"]):
                continue
            self.ghl.enroll(gid, "db_reactivation")
            self.ghl.enrollments[-1]["ctx"] = {"system": c["system"]}
            n += 1
        self.run_sequences()
        self.note(f"database reactivation enrolled {n}")
        return n

    # --------------------------------------------- FIELDPULSE CALENDAR -> CAMPAIGN
    def fill_calendar(self, window=(7, 14), open_threshold=0.5, max_targets=20, offer="$40 off"):
        """Look 7-14 days out. If any day is under-booked (>= open_threshold of capacity open),
        target past customers (by last service age) sized to the open slots."""
        today = self.clock.now.date()
        avail = self.fp.availability(today + timedelta(days=window[0]), today + timedelta(days=window[1]))
        slow = {d: n for d, n in avail.items()
                if self.fp.capacity(d) and n / self.fp.capacity(d) >= open_threshold}
        if not slow:
            self.note("fill_calendar: no slow days in window")
            return dict(slow={}, enrolled=0)
        open_slots = sum(slow.values())
        budget = min(max_targets, max(1, int(open_slots * 0.6)))   # ~1 booking per 3 texts, conservative
        days_txt = ", ".join(f"{d:%a %b %d}" for d in sorted(slow))
        season = SEASON_BY_MONTH[self.clock.now.month]
        pool = sorted((c for c in self.fp.customers.values()
                       if c["status"] == "customer" and c["last_service"]),
                      key=lambda c: c["last_service"])           # longest-since-service first
        n = 0
        for c in pool:
            if n >= budget:
                break
            gid, _ = self.ghl.upsert_contact(c["name"], c["phone"], c["email"], c["id"], tags={"past-customer"})
            g = self.ghl.contacts[gid]
            if g["dnd"] or self.ghl.is_enrolled(gid) or self.fp.has_open_work(c["id"]):
                continue
            self.ghl.enroll(gid, "fill_calendar")
            self.ghl.enrollments[-1]["ctx"] = dict(system=c["system"], season=season, offer=offer, days=days_txt)
            n += 1
        self.run_sequences()
        self.note(f"fill_calendar: slow days {days_txt} ({open_slots} open slots) -> enrolled {n}")
        return dict(slow=slow, enrolled=n)

"""Adapter layer. The engine only talks to these interfaces.
Mock* classes run the demo offline; Live* classes (see README) swap in real
GHL (LeadConnector API v2) and FieldPulse REST clients with the same methods."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def advance(self, **kw):
        self.now += timedelta(**kw)


# ---------------------------------------------------------------- FieldPulse
class MockFieldPulse:
    TECHS, SLOTS_PER_TECH = 3, 4          # 12 bookable slots / day
    OPEN_WEEKDAYS = {0, 1, 2, 3, 4, 5}    # Mon-Sat

    def __init__(self, clock: Clock):
        self.clock = clock
        self.customers, self.jobs, self.estimates, self.invoices = {}, [], {}, {}
        self._n = 0

    def _id(self, p):
        self._n += 1
        return f"{p}{self._n}"

    # customers
    def add_customer(self, name, phone, email, last_service=None, system="AC", status="customer"):
        cid = self._id("fpc")
        self.customers[cid] = dict(id=cid, name=name, phone=phone, email=email,
                                   last_service=last_service, system=system, status=status)
        return cid

    def find_customer(self, phone=None, email=None):
        for c in self.customers.values():
            if (phone and c["phone"] == phone) or (email and c["email"] == email):
                return c
        return None

    def create_lead(self, name, phone, email, source):
        return self.add_customer(name, phone, email, None, "unknown", status="lead")

    # schedule
    def capacity(self, d: date):
        return self.TECHS * self.SLOTS_PER_TECH if d.weekday() in self.OPEN_WEEKDAYS else 0

    def availability(self, start: date, end: date):
        """{date: open_slots} for each day in [start, end]"""
        out, d = {}, start
        while d <= end:
            booked = sum(1 for j in self.jobs if j["start"].date() == d and j["status"] != "cancelled")
            out[d] = max(self.capacity(d) - booked, 0)
            d += timedelta(days=1)
        return out

    def create_job(self, customer_id, start: datetime, jtype):
        jid = self._id("job")
        self.jobs.append(dict(id=jid, customer_id=customer_id, start=start, type=jtype, status="scheduled"))
        self.customers[customer_id]["status"] = "scheduled"
        return jid

    def seed_load(self, d: date, booked: int):
        for _ in range(booked):
            self.jobs.append(dict(id=self._id("job"), customer_id=None,
                                  start=datetime.combine(d, datetime.min.time()).replace(hour=9),
                                  type="seed", status="scheduled"))

    # estimates / invoices
    def create_estimate(self, customer_id, amount, sent_at):
        eid = self._id("est")
        self.estimates[eid] = dict(id=eid, customer_id=customer_id, amount=amount,
                                   status="sent", sent_at=sent_at)
        return eid

    def customer_state(self, cid):
        """Canonical status used for sync. Priority: paid > completed > converted > booked > lead"""
        if any(i["customer_id"] == cid and i["paid"] for i in self.invoices.values()):
            return "paid"
        if any(j["customer_id"] == cid and j["status"] == "completed" for j in self.jobs):
            return "completed"
        if any(e["customer_id"] == cid and e["status"] == "approved" for e in self.estimates.values()):
            return "converted"
        if any(j["customer_id"] == cid and j["status"] == "scheduled" for j in self.jobs):
            return "booked"
        return "lead"

    def has_open_work(self, cid):
        return self.customer_state(cid) in ("booked", "converted")


# ----------------------------------------------------------------------- GHL
class MockGHL:
    def __init__(self, clock: Clock):
        self.clock = clock
        self.contacts, self.messages, self.enrollments, self.opps = {}, [], [], {}
        self._n = 0

    def upsert_contact(self, name, phone, email, fp_id=None, tags=()):
        for c in self.contacts.values():
            if c["phone"] == phone or (email and c["email"] == email):
                c["tags"] |= set(tags)
                c["fp_id"] = c["fp_id"] or fp_id
                return c["id"], False
        self._n += 1
        cid = f"ghl{self._n}"
        self.contacts[cid] = dict(id=cid, name=name, phone=phone, email=email,
                                  fp_id=fp_id, tags=set(tags), dnd=False, fp_state=None)
        return cid, True

    def add_tag(self, cid, tag): self.contacts[cid]["tags"].add(tag)

    def send(self, cid, channel, text):
        c = self.contacts[cid]
        if c["dnd"] and channel == "sms":
            return False
        self.messages.append(dict(at=self.clock.now, cid=cid, name=c["name"], channel=channel,
                                  dir="out", text=text))
        return True

    def receive(self, cid, text):
        self.messages.append(dict(at=self.clock.now, cid=cid, name=self.contacts[cid]["name"],
                                  channel="sms", dir="in", text=text))

    def set_stage(self, cid, stage): self.opps[cid] = stage

    def enroll(self, cid, seq):
        if self.is_enrolled(cid, seq):
            return
        self.enrollments.append(dict(cid=cid, seq=seq, step=0, start=self.clock.now, active=True))

    def is_enrolled(self, cid, seq=None):
        return any(e["cid"] == cid and e["active"] and (seq is None or e["seq"] == seq)
                   for e in self.enrollments)

    def cancel(self, cid, seqs=None):
        n = 0
        for e in self.enrollments:
            if e["cid"] == cid and e["active"] and (seqs is None or e["seq"] in seqs):
                e["active"] = False
                n += 1
        return n

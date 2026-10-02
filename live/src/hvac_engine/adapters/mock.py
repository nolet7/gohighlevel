"""In-memory fakes for GHL and FieldPulse. Used by tests, the demo and `mode=mock`."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..domain import Channel, Customer, DayAvailability, Estimate, FPState
from ..ports import Clock


class MockCRM:
    def __init__(self) -> None:
        self.contacts: dict[str, dict] = {}
        self.sent: list[dict] = []
        self._keys: set[str] = set()
        self.stages: dict[str, str] = {}
        self.tags: dict[str, set[str]] = {}
        self.dnd: dict[str, bool] = {}
        self.fail_next_sends = 0   # fault injection for tests

    def upsert_contact(self, name, phone, email, tags):
        for cid, c in self.contacts.items():
            if (phone and c["phone"] == phone) or (email and c["email"] == email):
                self.tags[cid] |= set(tags)
                return cid, False
        cid = f"ghl{len(self.contacts) + 1}"
        self.contacts[cid] = dict(name=name, phone=phone, email=email)
        self.tags[cid] = set(tags)
        return cid, True

    def send(self, contact_id, channel: Channel, text, idempotency_key):
        if self.fail_next_sends > 0:
            self.fail_next_sends -= 1
            raise RuntimeError("simulated provider outage")
        if idempotency_key in self._keys:
            return True
        self._keys.add(idempotency_key)
        self.sent.append(dict(cid=contact_id, channel=channel.value, text=text, key=idempotency_key))
        return True

    def set_stage(self, contact_id, stage): self.stages[contact_id] = stage
    def add_tags(self, contact_id, tags): self.tags.setdefault(contact_id, set()).update(tags)
    def set_dnd(self, contact_id, dnd): self.dnd[contact_id] = dnd


class MockFieldPulse:
    TECHS, SLOTS_PER_TECH = 3, 4
    OPEN_WEEKDAYS = {0, 1, 2, 3, 4, 5}   # Mon-Sat

    def __init__(self, clock: Clock, tz: str = "America/New_York") -> None:
        self.clock, self.tz = clock, ZoneInfo(tz)
        self.customers: dict[str, Customer] = {}
        self.jobs: list[dict] = []
        self.estimates: dict[str, Estimate] = {}
        self.paid: set[str] = set()
        self._n = 0
        self.fail_job_creation = False

    def _id(self, p: str) -> str:
        self._n += 1
        return f"{p}{self._n}"

    # --- port
    def find_customer(self, phone, email):
        for c in self.customers.values():
            if (phone and c.phone == phone) or (email and c.email == email):
                return c
        return None

    def create_lead(self, name, phone, email, source):
        return self.add_customer(name, phone, email, is_lead=True)

    def get_customer(self, customer_id): return self.customers.get(customer_id)
    def list_customers(self): return list(self.customers.values())

    def capacity(self, d: date) -> int:
        return self.TECHS * self.SLOTS_PER_TECH if d.weekday() in self.OPEN_WEEKDAYS else 0

    def availability(self, start, end):
        out, d = {}, start
        while d <= end:
            booked = sum(1 for j in self.jobs
                         if j["start"].astimezone(self.tz).date() == d and j["status"] != "cancelled")
            cap = self.capacity(d)
            out[d] = DayAvailability(d, cap, max(cap - booked, 0))
            d += timedelta(days=1)
        return out

    def create_job(self, customer_id, start, job_type):
        if self.fail_job_creation:
            raise RuntimeError("FieldPulse 503")
        jid = self._id("job")
        self.jobs.append(dict(id=jid, customer_id=customer_id, start=start, type=job_type, status="scheduled"))
        return jid

    def customer_state(self, customer_id):
        if customer_id in self.paid:
            return FPState.PAID
        if any(j["customer_id"] == customer_id and j["status"] == "completed" for j in self.jobs):
            return FPState.COMPLETED
        if any(e.customer_id == customer_id and e.status == "approved" for e in self.estimates.values()):
            return FPState.CONVERTED
        if any(j["customer_id"] == customer_id and j["status"] == "scheduled" for j in self.jobs):
            return FPState.BOOKED
        return FPState.LEAD

    def unsold_estimates(self, min_age_days):
        cutoff = self.clock.now() - timedelta(days=min_age_days)
        return [e for e in self.estimates.values() if e.status == "sent" and e.sent_at <= cutoff]

    # --- test/demo helpers
    def add_customer(self, name, phone, email, last_service=None, system="HVAC", opt_in=True, is_lead=False):
        cid = self._id("fpc")
        self.customers[cid] = Customer(cid, name, phone, email, last_service, system, opt_in, is_lead)
        return cid

    def seed_load(self, d: date, booked: int):
        start = datetime(d.year, d.month, d.day, 9, tzinfo=self.tz)
        for _ in range(booked):
            self.jobs.append(dict(id=self._id("job"), customer_id=None, start=start, type="seed", status="scheduled"))

    def clear_days(self, start: date, end: date):
        self.jobs = [j for j in self.jobs if not (start <= j["start"].astimezone(self.tz).date() <= end)]

    def add_estimate(self, customer_id, amount, sent_at, status="sent"):
        eid = self._id("est")
        self.estimates[eid] = Estimate(eid, customer_id, amount, sent_at, status)
        return eid

    def set_estimate_status(self, eid, status):
        self.estimates[eid] = replace(self.estimates[eid], status=status)

    def complete_jobs(self, customer_id):
        for j in self.jobs:
            if j["customer_id"] == customer_id:
                j["status"] = "completed"

    def mark_paid(self, customer_id): self.paid.add(customer_id)

"""FieldPulse adapter.

Documented facts (help.fieldpulse.com/api-reference): base URL + `x-api-key` header; GET /jobs,
/customers, /estimates, /invoices with `page` (from 1) and `limit` (max 100); envelope
{error, total_count, response:[...]}; 50 req/s limit; webhooks for job status only; NO schedule or
availability endpoint.

Therefore:
  * availability = configured technician capacity - jobs scheduled that day (derived from /jobs)
  * all reads go through one cached snapshot (jobs/estimates/invoices/customers) so a tick costs a few
    paged calls instead of one per contact.

UNVERIFIED: the docs available to me do not list job/customer field names, so every field name lives in
`FieldMap` (best-guess defaults). `scripts/fieldpulse_probe.py` prints real payload keys and validates
the mapping against a live key before go-live.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from ..config import Settings
from ..domain import Customer, DayAvailability, Estimate, FPState, normalize_email, normalize_phone
from ..ports import Clock
from .http import request_with_retry

logger = logging.getLogger(__name__)


@dataclass
class FieldMap:
    # customers
    customer_id: str = "id"
    first_name: str = "first_name"
    last_name: str = "last_name"
    display_name: str = "display_name"
    email: str = "email"
    phone: str = "phone"
    marketing_opt_in: str = "sms_opt_in"      # if absent in payload, defaults to opted-in (CONFIRM with client)
    last_service: str = "last_service_date"   # if absent, derived from latest completed job
    # jobs
    job_id: str = "id"
    job_customer_id: str = "customer_id"
    job_status: str = "status"
    job_start: str = "start_time"
    job_created: str = "created_at"
    # estimates / invoices
    estimate_id: str = "id"
    estimate_customer_id: str = "customer_id"
    estimate_status: str = "status"
    estimate_amount: str = "total"
    estimate_created: str = "created_at"
    invoice_customer_id: str = "customer_id"
    invoice_status: str = "status"
    # status vocab (compared lowercase)
    completed_statuses: set[str] = field(default_factory=lambda: {"completed", "complete", "done"})
    cancelled_statuses: set[str] = field(default_factory=lambda: {"cancelled", "canceled"})
    approved_estimate_statuses: set[str] = field(default_factory=lambda: {"approved", "accepted"})
    unsold_estimate_statuses: set[str] = field(default_factory=lambda: {"sent", "pending", "open"})
    paid_invoice_statuses: set[str] = field(default_factory=lambda: {"paid"})


def parse_ts(v) -> datetime | None:
    """Accepts epoch seconds/millis or ISO-8601; returns aware UTC."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        n = float(v)
        return datetime.fromtimestamp(n / 1000 if n > 1e11 else n, UTC)
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


@dataclass
class _Snapshot:
    taken: datetime
    customers: dict[str, Customer]
    jobs: list[dict]
    estimates: list[dict]
    invoices: list[dict]


class FieldPulseClient:
    PAGE = 100

    def __init__(self, api_key: str, base_url: str, clock: Clock, tz: str, *, tech_count: int = 3,
                 slots_per_tech: int = 4, workdays: str = "0,1,2,3,4,5", ttl_seconds: int = 60,
                 fmap: FieldMap | None = None, http: httpx.Client | None = None):
        self.clock, self.tz = clock, ZoneInfo(tz)
        self.cap_per_day = tech_count * slots_per_tech
        self.workdays = {int(x) for x in workdays.split(",") if x.strip()}
        self.ttl, self.m = timedelta(seconds=ttl_seconds), fmap or FieldMap()
        self.http = http or httpx.Client(base_url=base_url, timeout=20.0,
                                         headers={"x-api-key": api_key, "Accept": "application/json"})
        self._snap: _Snapshot | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, s: Settings, clock: Clock) -> FieldPulseClient:
        assert s.fieldpulse_api_key
        return cls(s.fieldpulse_api_key.get_secret_value(), s.fieldpulse_base_url, clock, s.timezone,
                   tech_count=s.fp_tech_count, slots_per_tech=s.fp_slots_per_tech_per_day,
                   workdays=s.fp_workdays, ttl_seconds=s.fp_snapshot_ttl_seconds)

    # ------------------------------------------------------------- transport
    def _paged(self, path: str, params: dict | None = None) -> list[dict]:
        out, page = [], 1
        while True:
            q = {**(params or {}), "page": page, "limit": self.PAGE}
            data = request_with_retry(self.http, "fieldpulse", "GET", path, params=q).json()
            rows = data.get("response") or []
            out.extend(rows)
            if not rows or len(out) >= int(data.get("total_count", len(out))):
                return out
            page += 1

    def _customer(self, r: dict, last_service: date | None) -> Customer:
        m = self.m
        name = (f"{r.get(m.first_name) or ''} {r.get(m.last_name) or ''}".strip()
                or r.get(m.display_name) or "Customer")
        ls = parse_ts(r.get(m.last_service))
        return Customer(
            id=str(r[m.customer_id]), name=name, phone=normalize_phone(r.get(m.phone)),
            email=normalize_email(r.get(m.email)),
            last_service=ls.astimezone(self.tz).date() if ls else last_service,
            marketing_opt_in=bool(r.get(m.marketing_opt_in, True)),
        )

    def snapshot(self, force: bool = False) -> _Snapshot:
        with self._lock:
            now = self.clock.now()
            if self._snap and not force and now - self._snap.taken < self.ttl:
                return self._snap
            jobs = self._paged("/jobs")
            estimates = self._paged("/estimates")
            invoices = self._paged("/invoices")
            m, latest_done = self.m, {}
            for j in jobs:
                st = parse_ts(j.get(m.job_start))
                if str(j.get(m.job_status, "")).lower() in m.completed_statuses and st:
                    cid = str(j.get(m.job_customer_id))
                    d = st.astimezone(self.tz).date()
                    latest_done[cid] = max(latest_done.get(cid, d), d)
            customers = {str(r[m.customer_id]): self._customer(r, latest_done.get(str(r[m.customer_id])))
                         for r in self._paged("/customers")}
            self._snap = _Snapshot(now, customers, jobs, estimates, invoices)
            return self._snap

    # ------------------------------------------------------------------ port
    def find_customer(self, phone, email):
        for c in self.snapshot().customers.values():
            if (phone and c.phone == phone) or (email and c.email == email):
                return c
        return None

    def get_customer(self, customer_id):
        return self.snapshot().customers.get(customer_id)

    def list_customers(self):
        return list(self.snapshot().customers.values())

    def create_lead(self, name, phone, email, source):
        parts = name.split(None, 1)
        body = {self.m.first_name: parts[0], self.m.last_name: parts[1] if len(parts) > 1 else "",
                self.m.email: email, self.m.phone: phone}
        data = request_with_retry(self.http, "fieldpulse", "POST", "/customers", json=body).json()
        resp = data.get("response", data)
        self._snap = None   # force re-read next time
        return str(resp[self.m.customer_id])

    def create_job(self, customer_id, start, job_type):
        body = {self.m.job_customer_id: customer_id, self.m.job_start: int(start.timestamp()),
                "title": f"{job_type} (auto-booked)"}
        data = request_with_retry(self.http, "fieldpulse", "POST", "/jobs", json=body).json()
        self._snap = None
        resp = data.get("response", data)
        return str(resp[self.m.job_id])

    def availability(self, start: date, end: date) -> dict[date, DayAvailability]:
        m, booked = self.m, {}
        for j in self.snapshot().jobs:
            st = parse_ts(j.get(m.job_start))
            if not st or str(j.get(m.job_status, "")).lower() in m.cancelled_statuses:
                continue
            d = st.astimezone(self.tz).date()
            booked[d] = booked.get(d, 0) + 1
        out, d = {}, start
        while d <= end:
            cap = self.cap_per_day if d.weekday() in self.workdays else 0
            out[d] = DayAvailability(d, cap, max(cap - booked.get(d, 0), 0))
            d += timedelta(days=1)
        return out

    def customer_state(self, customer_id) -> FPState:
        m, snap = self.m, self.snapshot()
        if any(str(i.get(m.invoice_customer_id)) == customer_id
               and str(i.get(m.invoice_status, "")).lower() in m.paid_invoice_statuses for i in snap.invoices):
            return FPState.PAID
        jobs = [j for j in snap.jobs if str(j.get(m.job_customer_id)) == customer_id]
        statuses = {str(j.get(m.job_status, "")).lower() for j in jobs}
        if statuses & m.completed_statuses:
            return FPState.COMPLETED
        if any(str(e.get(m.estimate_customer_id)) == customer_id
               and str(e.get(m.estimate_status, "")).lower() in m.approved_estimate_statuses for e in snap.estimates):
            return FPState.CONVERTED
        if statuses - m.cancelled_statuses:
            return FPState.BOOKED
        return FPState.LEAD

    def unsold_estimates(self, min_age_days) -> list[Estimate]:
        m, cutoff = self.m, self.clock.now() - timedelta(days=min_age_days)
        out = []
        for e in self.snapshot().estimates:
            sent = parse_ts(e.get(m.estimate_created))
            if sent and sent <= cutoff and str(e.get(m.estimate_status, "")).lower() in m.unsold_estimate_statuses:
                out.append(Estimate(str(e[m.estimate_id]), str(e.get(m.estimate_customer_id)),
                                    float(e.get(m.estimate_amount) or 0), sent, "sent"))
        return out

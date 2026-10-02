"""Reactivation and calendar-fill campaigns. Every campaign supports dry_run (plan without sending)."""
from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from ..domain import STOPPING_STATES, ContactRecord, Customer, normalize_email, normalize_phone
from ..sequences import SEASON_BY_MONTH
from .context import Deps
from .runner import SequenceRunner

logger = logging.getLogger(__name__)


@dataclass
class CampaignPlan:
    name: str
    dry_run: bool
    enrolled: int = 0
    candidates: int = 0
    budget: int | None = None
    slow_days: dict[str, int] = field(default_factory=dict)   # ISO date -> open slots
    open_slots: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    targets: list[dict] = field(default_factory=list)          # first name + ids only (no contact PII)
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class CampaignService:
    def __init__(self, d: Deps, runner: SequenceRunner):
        self.d, self.runner = d, runner

    # ------------------------------------------------------------ eligibility
    def _skip_reason(self, c: Customer) -> str | None:
        d = self.d
        if not c.marketing_opt_in:
            return "no_marketing_opt_in"
        if not normalize_phone(c.phone) and not normalize_email(c.email):
            return "no_contact_info"
        rec = d.store.contact_by_fp(c.id)
        if rec:
            if rec.dnd:
                return "dnd"
            if d.store.is_enrolled(rec.id):
                return "already_enrolled"
            week_ago = d.clock.now() - timedelta(days=7)
            if d.store.marketing_sent_since(rec.id, week_ago) >= d.settings.marketing_cap_per_week:
                return "weekly_cap"
        if d.fs.customer_state(c.id) in STOPPING_STATES:
            return "open_work_in_fieldpulse"
        return None

    def _enroll(self, plan: CampaignPlan, customers: list[Customer], sequence: str, limit: int, ctx: dict) -> None:
        d = self.d
        for c in customers:
            if plan.enrolled >= limit:
                break
            reason = self._skip_reason(c)
            if reason:
                plan.skipped[reason] = plan.skipped.get(reason, 0) + 1
                continue
            plan.targets.append({"fp_customer_id": c.id, "first_name": (c.name.split() or [""])[0]})
            plan.enrolled += 1
            if plan.dry_run:
                continue
            phone, email = normalize_phone(c.phone), normalize_email(c.email)
            cid, _ = d.crm.upsert_contact(c.name, phone, email, {"past-customer"})
            rec = d.store.get_contact(cid) or ContactRecord(id=cid, name=c.name, phone=phone, email=email)
            rec.fp_id = c.id
            rec.sms_consent = rec.sms_consent or c.marketing_opt_in
            rec.tags.add("past-customer")
            d.store.save_contact(rec, d.clock.now())
            self.runner.enroll(cid, sequence, {**ctx, "system": c.system})

    def _finish(self, plan: CampaignPlan) -> CampaignPlan:
        d = self.d
        if not plan.dry_run:
            self.runner.run_due()
            d.metrics.inc("campaign_runs_total", name=plan.name)
            d.store.audit(d.clock.now(), "campaign", f"campaign_{plan.name}", None,
                          enrolled=plan.enrolled, skipped=plan.skipped, slow_days=plan.slow_days)
        logger.info("campaign %s dry_run=%s enrolled=%s", plan.name, plan.dry_run, plan.enrolled)
        return plan

    # ---------------------------------------------------- calendar-fill (core)
    def fill_calendar(self, dry_run: bool = False, force: bool = False) -> CampaignPlan:
        """Read FieldPulse availability N..M days out; if days are under-booked, message the
        longest-lapsed past customers, sized to the open capacity."""
        d, s = self.d, self.d.settings
        plan = CampaignPlan("fill_calendar", dry_run)
        tz = d.policy.tz
        today = d.clock.now().astimezone(tz).date()
        avail = d.fs.availability(today + timedelta(days=s.fill_window_start_days),
                                  today + timedelta(days=s.fill_window_end_days))
        slow = {day: a for day, a in avail.items() if a.capacity and a.open_ratio >= s.fill_open_threshold}
        plan.slow_days = {day.isoformat(): a.open for day, a in sorted(slow.items())}
        plan.open_slots = sum(a.open for a in slow.values())
        if not slow:
            plan.note = "no slow days in window"
            return self._finish(plan)

        workdays = [a for a in avail.values() if a.capacity]
        if workdays and len(slow) / len(workdays) > s.fill_max_slow_day_fraction and not force:
            # An empty-looking calendar is far more likely to be an API outage or a wrong capacity setting
            # than a genuinely empty week. Refuse to blast the database on bad data.
            plan.note = (f"anomaly: {len(slow)} of {len(workdays)} workdays look slow (>"
                         f"{s.fill_max_slow_day_fraction:.0%}); verify FieldPulse data and capacity settings, "
                         "then re-run with force=true")
            d.metrics.inc("campaign_blocked_total", reason="calendar_anomaly")
            if not dry_run:
                d.store.audit(d.clock.now(), "campaign", "campaign_blocked_anomaly", None,
                              slow=len(slow), workdays=len(workdays))
            logger.warning(plan.note)
            return plan

        run_key = f"fill:{today.isoformat()}:{','.join(sorted(plan.slow_days))}"
        if not dry_run and not force and not d.store.first_time(run_key, d.clock.now()):
            plan.note = "already ran today for these slow days (use force=true to override)"
            return plan

        want_bookings = math.ceil(plan.open_slots * s.fill_target_fill_fraction)
        plan.budget = min(s.fill_max_targets, max(1, math.ceil(want_bookings / s.fill_expected_booking_rate)))
        cutoff = today - timedelta(days=30 * s.fill_min_months_since_service)
        pool = sorted((c for c in d.fs.list_customers()
                       if c.last_service and c.last_service <= cutoff and not c.is_lead),
                      key=lambda c: c.last_service)
        plan.candidates = len(pool)
        days_txt = ", ".join(day.strftime("%a %b %d").replace(" 0", " ") for day in sorted(slow))
        ctx = {"days": days_txt, "season": SEASON_BY_MONTH[today.month], "offer": s.fill_offer}
        self._enroll(plan, pool, "fill_calendar", plan.budget, ctx)
        return self._finish(plan)

    # -------------------------------------------------- unsold estimates
    def reactivate_estimates(self, dry_run: bool = False, min_age_days: int = 5, limit: int = 50) -> CampaignPlan:
        plan = CampaignPlan("estimate_reactivation", dry_run)
        by_id = {c.id: c for c in self.d.fs.list_customers()}
        customers = [by_id[e.customer_id] for e in self.d.fs.unsold_estimates(min_age_days) if e.customer_id in by_id]
        plan.candidates = len(customers)
        self._enroll(plan, customers, "estimate_reactivation", limit, {})
        return self._finish(plan)

    # ------------------------------------------------- database reactivation
    def reactivate_database(self, dry_run: bool = False, min_months: int = 11, limit: int = 50) -> CampaignPlan:
        plan = CampaignPlan("db_reactivation", dry_run)
        today = self.d.clock.now().astimezone(self.d.policy.tz).date()
        cutoff = today - timedelta(days=30 * min_months)
        pool = sorted((c for c in self.d.fs.list_customers()
                       if c.last_service and c.last_service <= cutoff and not c.is_lead),
                      key=lambda c: c.last_service)
        plan.candidates = len(pool)
        self._enroll(plan, pool, "db_reactivation", limit, {})
        return self._finish(plan)

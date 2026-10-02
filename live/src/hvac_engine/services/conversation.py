"""Inbound conversation handling: opt-out keywords, safety escalation, qualification, booking.

In production GHL Conversation AI can own the open-ended dialogue; this service is the
deterministic spine around it (compliance keywords, safety, double-booking guard, FieldPulse booking).
`Qualifier` is the seam where an LLM-based classifier can replace the rule-based one.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from ..domain import STOPPING_STATES, Channel, ContactRecord
from ..logging import log
from .context import Deps
from .runner import SequenceRunner
from .sync import SyncService

logger = logging.getLogger(__name__)

STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
START_WORDS = {"start", "unstop"}
HELP_WORDS = {"help", "info"}
SAFETY_PHRASES = ("smell gas", "gas leak", "gas smell", "smells like gas", "carbon monoxide", "co alarm")
URGENT_PHRASES = ("no heat", "no cool", "no ac", "not cooling", "not heating", "not working",
                  "water leak", "leaking", "burning smell", "emergency")
BOOK_WORDS = {"yes", "yep", "yeah", "book", "schedule", "appointment", "tomorrow", "today",
              "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"}


@dataclass(frozen=True)
class Intent:
    urgent: bool
    wants_booking: bool
    safety: bool


class Qualifier(Protocol):
    def classify(self, text: str) -> Intent: ...


class RuleBasedQualifier:
    def classify(self, text: str) -> Intent:
        low = text.lower()
        words = set(re.findall(r"[a-z]+", low))
        return Intent(
            urgent=any(p in low for p in URGENT_PHRASES),
            wants_booking=bool(words & BOOK_WORDS),
            safety=any(p in low for p in SAFETY_PHRASES),
        )


def _keyword(text: str) -> str:
    return re.sub(r"[^\w\s]", "", text.lower()).strip()


class ConversationService:
    def __init__(self, d: Deps, runner: SequenceRunner, sync: SyncService, qualifier: Qualifier | None = None):
        self.d, self.runner, self.sync = d, runner, sync
        self.qualifier = qualifier or RuleBasedQualifier()

    # ------------------------------------------------------------ helpers
    def _reply(self, c: ContactRecord, text: str, kind: str = "service") -> None:
        """Replies to an inbound message: allowed during quiet hours, never to a DND contact
        (except the single STOP confirmation, which carriers require)."""
        d = self.d
        now = d.clock.now()
        key = f"reply:{c.id}:{now.isoformat()}:{abs(hash(text)) % 10**8}"
        if d.crm.send(c.id, Channel.SMS, text, key):
            d.store.record_message(key, c.id, "out", "sms", kind, text, now)

    def _local_fmt(self, dt: datetime) -> str:
        return dt.astimezone(self.d.policy.tz).strftime("%A %b %d at %I:%M %p").replace(" 0", " ")

    # ------------------------------------------------------- missed call
    def missed_call(self, phone: str, name: str = "there") -> str:
        d = self.d
        from ..domain import normalize_phone
        p = normalize_phone(phone)
        if not p:
            raise ValueError("invalid phone")
        now = d.clock.now()
        cid, _ = d.crm.upsert_contact(name, p, None, {"source:missed_call"})
        c = d.store.get_contact(cid) or ContactRecord(id=cid, name=name, phone=p, email=None)
        c.tags.add("source:missed_call")
        if d.settings.missed_call_implied_consent and not c.dnd:
            c.sms_consent = True
        d.store.save_contact(c, now)
        d.store.audit(now, "system", "missed_call", cid)
        if not c.dnd and c.sms_consent:
            self._reply(c, f"Hi, sorry we missed your call! This is {d.settings.company_name}. "
                           "Text us what's going on and we'll get you scheduled. Reply STOP to opt out.")
        d.metrics.inc("missed_calls_total")
        return cid

    # ----------------------------------------------------------- inbound
    def handle_inbound(self, contact_id: str, text: str) -> str:
        d = self.d
        c = d.store.get_contact(contact_id)
        if c is None:
            raise KeyError(contact_id)
        now = d.clock.now()
        d.store.record_message(None, c.id, "in", "sms", "inbound", text[:1000], now)
        kw = _keyword(text)

        if kw in STOP_WORDS:
            c.dnd = True
            d.store.save_contact(c, now)
            d.store.cancel_enrollments(c.id, "opt_out", now)
            d.crm.set_dnd(c.id, True)
            d.crm.add_tags(c.id, {"dnd"})
            d.store.audit(now, "contact", "opt_out", c.id)
            d.metrics.inc("opt_outs_total")
            self._reply(c, f"You're unsubscribed from {d.settings.company_name} texts. Reply START to resubscribe.")
            return "opted_out"
        if kw in START_WORDS:
            c.dnd, c.sms_consent = False, True
            d.store.save_contact(c, now)
            d.crm.set_dnd(c.id, False)
            d.store.audit(now, "contact", "opt_in", c.id)
            self._reply(c, f"Welcome back! You're subscribed to {d.settings.company_name} texts. "
                           "Reply STOP to opt out.")
            return "opted_in"
        if c.dnd:
            return "ignored_dnd"
        if kw in HELP_WORDS:
            self._reply(c, f"{d.settings.company_name}: reply BOOK to schedule service, STOP to opt out. "
                           "Msg&data rates may apply.")
            return "help"

        if not c.sms_consent:       # they texted us first: that is an inquiry
            c.sms_consent = True
            d.store.save_contact(c, now)

        intent = self.qualifier.classify(text)
        if intent.safety:
            d.crm.add_tags(c.id, {"needs-human", "safety"})
            d.store.audit(now, "system", "safety_escalation", c.id)
            d.metrics.inc("safety_escalations_total")
            self._reply(c, "If you smell gas or your CO alarm is going off: leave the building now and call 911 "
                           "and your gas utility from outside. Do not use switches or phones inside. "
                           "A team member is being alerted to follow up with you.")
            return "safety_escalation"

        if intent.urgent or intent.wants_booking:
            return self._book(c, intent.urgent)

        self._reply(c, "Thanks! Is your system heating or cooling, and when did the problem start? "
                       "I can also get you on the schedule, just say BOOK.")
        return "qualifying"

    # ----------------------------------------------------------- booking
    def _next_slot(self, urgent: bool) -> datetime | None:
        d = self.d
        tz = d.policy.tz
        now_local = d.clock.now().astimezone(tz)
        today = now_local.date()
        start = today if urgent else today + timedelta(days=1)
        for day, av in sorted(d.fs.availability(start, start + timedelta(days=14)).items()):
            if av.open <= 0:
                continue
            if day == today:
                hour = now_local.hour + 2
                if hour > 17:
                    continue
            else:
                hour = 8 + (av.capacity - av.open) % 9
            return datetime(day.year, day.month, day.day, hour, 0, tzinfo=tz)
        return None

    def _book(self, c: ContactRecord, urgent: bool) -> str:
        d = self.d
        now = d.clock.now()
        if not c.fp_id:
            c.fp_id = d.fs.create_lead(c.name, c.phone, c.email, "inbound_sms")
            d.store.save_contact(c, now)
        if d.fs.customer_state(c.fp_id) in STOPPING_STATES:
            self._reply(c, "You're already on our schedule. Reply here if you need to change anything.")
            return "already_booked"
        slot = self._next_slot(urgent)
        if slot is None:
            d.crm.add_tags(c.id, {"needs-human"})
            self._reply(c, "We're fully booked in the next two weeks, but I've alerted the team "
                           "and someone will call you shortly.")
            return "no_availability"
        try:
            d.fs.create_job(c.fp_id, slot, "emergency" if urgent else "service")
        except Exception:
            logger.exception("FieldPulse booking failed")
            d.crm.add_tags(c.id, {"needs-human"})
            d.metrics.inc("booking_failures_total")
            self._reply(c, "Thanks! I couldn't lock in a time automatically; "
                           "a team member will text you shortly to confirm.")
            return "booking_failed"
        d.crm.add_tags(c.id, {"ai-booked"})
        d.store.audit(now, "ai", "booked", c.id, slot=slot.isoformat(), urgent=urgent)
        d.metrics.inc("ai_bookings_total", urgent=str(urgent).lower())
        log(logger, logging.INFO, "booked", contact=c.id, urgent=urgent)
        self._reply(c, f"You're booked, {c.first_name}! A tech will arrive {self._local_fmt(slot)}. "
                       "Reply here if you need to change it.")
        self.sync.sync_contact(c.id)     # stops follow-ups immediately
        return "booked"

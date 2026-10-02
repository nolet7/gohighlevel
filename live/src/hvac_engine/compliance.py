"""Send-time policy: consent, DND, quiet hours and frequency caps.

Decisions are returned as a Verdict so callers can act correctly:
  ALLOW  -> send now
  DEFER  -> try again later (quiet hours, weekly cap)
  SKIP   -> never send this step (no consent for that channel)
  STOP   -> end the enrollment (contact opted out)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from .config import Settings
from .domain import Channel, ContactRecord
from .store import Store


class Verdict(StrEnum):
    ALLOW = "allow"
    DEFER = "defer"
    SKIP = "skip"
    STOP = "stop"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str = ""


TEXT_CHANNELS = {Channel.SMS, Channel.RVM, Channel.IMESSAGE}


class Policy:
    def __init__(self, settings: Settings, store: Store):
        self.s = settings
        self.store = store
        self.tz = ZoneInfo(settings.timezone)

    def local(self, now: datetime) -> datetime:
        return now.astimezone(self.tz)

    def in_quiet_hours(self, now: datetime) -> bool:
        h = self.local(now).hour
        start, end = self.s.quiet_hours_start, self.s.quiet_hours_end
        return (h >= start or h < end) if start > end else (start <= h < end)

    def evaluate(self, contact: ContactRecord, channel: Channel, now: datetime,
                 marketing: bool = True) -> Decision:
        if contact.dnd:
            return Decision(Verdict.STOP, "dnd")
        if channel in TEXT_CHANNELS and not contact.sms_consent:
            return Decision(Verdict.SKIP, "no_text_consent")
        if channel == Channel.SMS and not contact.phone:
            return Decision(Verdict.SKIP, "no_phone")
        if channel == Channel.EMAIL and not contact.email:
            return Decision(Verdict.SKIP, "no_email")
        if self.in_quiet_hours(now):
            return Decision(Verdict.DEFER, "quiet_hours")
        if marketing:
            sent = self.store.marketing_sent_since(contact.id, now - timedelta(days=7))
            if sent >= self.s.marketing_cap_per_week:
                return Decision(Verdict.DEFER, "weekly_cap")
        return Decision(Verdict.ALLOW)

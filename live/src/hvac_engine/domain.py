"""Pure domain types: no I/O."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class Channel(StrEnum):
    SMS = "sms"
    EMAIL = "email"
    RVM = "rvm"            # ringless voicemail
    IMESSAGE = "imessage"  # via Sendblue


class FPState(StrEnum):
    """Canonical FieldPulse lifecycle state. Priority: PAID > COMPLETED > CONVERTED > BOOKED > LEAD."""
    LEAD = "lead"
    BOOKED = "booked"
    CONVERTED = "converted"
    COMPLETED = "completed"
    PAID = "paid"


# States for which every marketing sequence must stop.
STOPPING_STATES = frozenset({FPState.BOOKED, FPState.CONVERTED, FPState.COMPLETED, FPState.PAID})

STAGE_FOR_STATE = {
    FPState.LEAD: "New Lead",
    FPState.BOOKED: "Booked",
    FPState.CONVERTED: "Won - Install",
    FPState.COMPLETED: "Job Complete",
    FPState.PAID: "Paid / Review Request",
}


@dataclass(frozen=True)
class Customer:
    """A FieldPulse customer record (the system of record)."""
    id: str
    name: str
    phone: str | None
    email: str | None
    last_service: date | None = None
    system: str = "HVAC"
    marketing_opt_in: bool = True
    is_lead: bool = False


@dataclass(frozen=True)
class Estimate:
    id: str
    customer_id: str
    amount: float
    sent_at: datetime
    status: str  # sent | approved | declined


@dataclass(frozen=True)
class DayAvailability:
    day: date
    capacity: int
    open: int

    @property
    def open_ratio(self) -> float:
        return self.open / self.capacity if self.capacity else 0.0


@dataclass
class ContactRecord:
    """Local mirror of a CRM contact plus the compliance/lifecycle flags we own."""
    id: str
    name: str
    phone: str | None
    email: str | None
    fp_id: str | None = None
    issue: str = "HVAC issue"
    dnd: bool = False
    sms_consent: bool = False
    fp_state: FPState = FPState.LEAD
    tags: set[str] = field(default_factory=set)

    @property
    def first_name(self) -> str:
        return (self.name.split() or ["there"])[0]


@dataclass(frozen=True)
class Step:
    delay_hours: float
    channel: Channel
    template: str


@dataclass(frozen=True)
class LeadIn:
    source: str
    name: str
    phone: str | None
    email: str | None
    issue: str = "HVAC issue"
    sms_consent: bool = False


_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


def normalize_phone(raw: str | None, default_country: str = "1") -> str | None:
    """Return E.164 or None. US-centric default (10-digit -> +1)."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if raw.strip().startswith("+"):
        cand = "+" + digits
    elif len(digits) == 10:
        cand = f"+{default_country}{digits}"
    elif len(digits) == 11 and digits.startswith(default_country):
        cand = f"+{digits}"
    else:
        return None
    return cand if _E164.match(cand) else None


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(raw: str | None) -> str | None:
    if not raw:
        return None
    e = raw.strip().lower()
    return e if _EMAIL.match(e) else None

"""Ports (interfaces). The engine depends only on these; adapters implement them.

CRMPort          -> GoHighLevel (contacts, messaging, pipeline stages, tags)
FieldServicePort -> FieldPulse (system of record for customers, jobs, estimates, schedule)
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Protocol

from .domain import Channel, Customer, DayAvailability, Estimate, FPState


class Clock(Protocol):
    def now(self) -> datetime: ...  # timezone-aware UTC


class CRMPort(Protocol):
    def upsert_contact(self, name: str, phone: str | None, email: str | None,
                       tags: set[str]) -> tuple[str, bool]:
        """Return (crm_contact_id, created)."""

    def send(self, contact_id: str, channel: Channel, text: str, idempotency_key: str) -> bool:
        """Deliver a message. Must be safe to call twice with the same key."""

    def set_stage(self, contact_id: str, stage: str) -> None: ...

    def add_tags(self, contact_id: str, tags: set[str]) -> None: ...

    def set_dnd(self, contact_id: str, dnd: bool) -> None: ...


class FieldServicePort(Protocol):
    def find_customer(self, phone: str | None, email: str | None) -> Customer | None: ...

    def create_lead(self, name: str, phone: str | None, email: str | None, source: str) -> str: ...

    def get_customer(self, customer_id: str) -> Customer | None: ...

    def list_customers(self) -> list[Customer]: ...

    def availability(self, start: date, end: date) -> dict[date, DayAvailability]: ...

    def create_job(self, customer_id: str, start: datetime, job_type: str) -> str: ...

    def customer_state(self, customer_id: str) -> FPState: ...

    def unsold_estimates(self, min_age_days: int) -> list[Estimate]: ...

from __future__ import annotations

from datetime import date, timedelta

from hvac_engine.domain import LeadIn


def make_lead(engine, name="Maria Lopez", phone="+14045550101", email="maria@example.com",
              consent=True, source="website", issue="AC not cooling") -> str:
    return engine.intake.ingest(LeadIn(source, name, phone, email, issue, consent))


def sent_to(crm, cid):
    return [m for m in crm.sent if m["cid"] == cid]


def seed_customers(fs, n=60, opt_in=True):
    ids = []
    for i in range(n):
        ids.append(fs.add_customer(f"Customer{i} Smith", f"+1404555{1000 + i}", f"c{i}@example.com",
                                   last_service=date(2025, 9, 1) - timedelta(days=i * 20), opt_in=opt_in))
    return ids


def busy_calendar(fs, clock, slow_offsets=(8, 11), days=17):
    """Everything nearly full except the slow offsets (days from today, local)."""
    from tests.conftest import TZ
    today = clock.now().astimezone(TZ).date()
    for off in range(days):
        fs.seed_load(today + timedelta(days=off), 3 if off in slow_offsets else 11)
    return today

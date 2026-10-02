"""Lead intake: website form, Meta Lead Ads and Google LSA all normalize into LeadIn."""
from __future__ import annotations

from ..domain import (
    STAGE_FOR_STATE,
    STOPPING_STATES,
    ContactRecord,
    FPState,
    LeadIn,
    normalize_email,
    normalize_phone,
)
from .context import Deps
from .runner import SequenceRunner


class IntakeService:
    def __init__(self, d: Deps, runner: SequenceRunner):
        self.d, self.runner = d, runner

    def ingest(self, lead: LeadIn) -> str:
        d = self.d
        phone, email = normalize_phone(lead.phone), normalize_email(lead.email)
        if not phone and not email:
            raise ValueError("lead needs a valid phone or email")
        if not lead.name.strip():
            raise ValueError("lead needs a name")
        now = d.clock.now()

        customer = d.fs.find_customer(phone, email)
        fp_id = customer.id if customer else d.fs.create_lead(lead.name.strip(), phone, email, lead.source)
        tags = {f"source:{lead.source}"}
        contact_id, _ = d.crm.upsert_contact(lead.name.strip(), phone, email, tags)

        rec = d.store.get_contact(contact_id) or ContactRecord(
            id=contact_id, name=lead.name.strip(), phone=phone, email=email)
        rec.fp_id, rec.issue = fp_id, lead.issue[:200]
        rec.sms_consent = rec.sms_consent or lead.sms_consent
        rec.tags |= tags
        state = d.fs.customer_state(fp_id)
        rec.fp_state = state
        d.store.save_contact(rec, now)

        enrolled = False
        if state not in STOPPING_STATES and not rec.dnd:
            d.crm.set_stage(contact_id, STAGE_FOR_STATE[FPState.LEAD])
            enrolled = self.runner.enroll(contact_id, "speed_to_lead")
        d.metrics.inc("leads_ingested_total", source=lead.source)
        d.store.audit(now, "system", "lead_ingested", contact_id, source=lead.source,
                      fp_id=fp_id, sms_consent=rec.sms_consent, enrolled=enrolled, existing_customer=bool(customer))
        self.runner.run_due(contact_id)   # fire the delay-0 steps right now
        return contact_id

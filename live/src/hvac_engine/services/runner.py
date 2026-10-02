"""Executes sequence steps. Called every tick and immediately after enrollment (speed-to-lead)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from ..compliance import Verdict
from ..domain import STOPPING_STATES, ContactRecord
from ..logging import log
from ..sequences import MARKETING_SEQUENCES, SEQUENCES, TRANSACTIONAL
from .context import Deps

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 5


class _Safe(dict):
    def __missing__(self, key: str) -> str:  # unknown placeholders render empty, never crash a send
        return ""


class SequenceRunner:
    def __init__(self, d: Deps):
        self.d = d

    def enroll(self, contact_id: str, sequence: str, ctx: dict | None = None) -> bool:
        if sequence not in SEQUENCES:
            raise ValueError(f"unknown sequence {sequence}")
        eid = self.d.store.enroll(contact_id, sequence, ctx or {}, self.d.clock.now())
        if eid:
            self.d.metrics.inc("enrollments_total", sequence=sequence)
            self.d.store.audit(self.d.clock.now(), "system", "enrolled", contact_id, sequence=sequence)
        return eid is not None

    def run_due(self, contact_id: str | None = None) -> int:
        sent = 0
        for row in self.d.store.active_enrollments():
            if contact_id and row["contact_id"] != contact_id:
                continue
            try:
                sent += self._run_one(row)
            except Exception:  # one bad enrollment must never block the rest
                logger.exception("enrollment %s failed", row["id"])
                self.d.metrics.inc("runner_errors_total")
        return sent

    def render(self, template: str, contact: ContactRecord, ctx: dict) -> str:
        data = _Safe(first=contact.first_name, co=self.d.settings.company_name, issue=contact.issue)
        data.update(ctx)
        return template.format_map(data)

    def _run_one(self, row) -> int:
        d = self.d
        steps = SEQUENCES[row["sequence"]]
        contact = d.store.get_contact(row["contact_id"])
        eid, seq = row["id"], row["sequence"]
        if contact is None:
            d.store.end_enrollment(eid, "failed", "missing_contact", d.clock.now())
            return 0
        # Delays are measured from when the previous step actually went out, so a deferral
        # (quiet hours, outage) never causes several steps to burst out together afterwards.
        last = datetime.fromisoformat(row["last_step_at"] or row["started_at"])
        ctx = json.loads(row["ctx"])
        marketing = seq in MARKETING_SEQUENCES
        idx, sent = row["step"], 0

        while idx < len(steps):
            step = steps[idx]
            now = d.clock.now()
            gap = step.delay_hours - (steps[idx - 1].delay_hours if idx else 0)
            if now < last + timedelta(hours=gap):
                return sent
            # defense in depth: even if a sync was missed, never market to a booked/paid customer
            if seq not in TRANSACTIONAL and contact.fp_state in STOPPING_STATES:
                d.store.end_enrollment(eid, "cancelled", f"fp_state:{contact.fp_state}", now)
                return sent
            dec = d.policy.evaluate(contact, step.channel, now, marketing)
            if dec.verdict is Verdict.STOP:
                d.store.end_enrollment(eid, "cancelled", dec.reason, now)
                return sent
            if dec.verdict is Verdict.DEFER:
                d.metrics.inc("sends_deferred_total", reason=dec.reason)
                return sent
            if dec.verdict is Verdict.SKIP:
                d.store.audit(now, "system", "step_skipped", contact.id, sequence=seq, step=idx, reason=dec.reason)
                d.metrics.inc("sends_skipped_total", reason=dec.reason)
                d.store.advance(eid, now)
                last = now
                idx += 1
                continue

            text = self.render(step.template, contact, ctx)
            key = f"enr{eid}:step{idx}"
            try:
                ok = d.crm.send(contact.id, step.channel, text, key)
            except Exception:
                ok = False
                logger.exception("send failed for enrollment %s", eid)
            if not ok:
                if d.store.bump_attempt(eid) >= MAX_ATTEMPTS:
                    d.store.end_enrollment(eid, "failed", "send_failed", now)
                d.metrics.inc("send_failures_total", channel=step.channel.value)
                return sent
            d.store.record_message(key, contact.id, "out", step.channel.value,
                                   "marketing" if marketing else "service", text, now)
            d.metrics.inc("messages_sent_total", channel=step.channel.value, sequence=seq)
            log(logger, logging.INFO, "step sent", sequence=seq, step=idx, channel=step.channel.value)
            d.store.advance(eid, now)
            last = now
            idx += 1
            sent += 1

        d.store.end_enrollment(eid, "completed", "done", d.clock.now())
        return sent

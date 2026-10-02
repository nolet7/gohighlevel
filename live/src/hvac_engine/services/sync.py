"""FieldPulse -> GHL status sync. FieldPulse is the system of record."""
from __future__ import annotations

import logging

from ..domain import STAGE_FOR_STATE, STOPPING_STATES
from ..logging import log
from ..sequences import STOPPABLE
from .context import Deps
from .runner import SequenceRunner

logger = logging.getLogger(__name__)


class SyncService:
    def __init__(self, d: Deps, runner: SequenceRunner):
        self.d, self.runner = d, runner

    def sync_contact(self, contact_id: str) -> bool:
        """Reconcile one contact. Returns True if the state changed."""
        d = self.d
        c = d.store.get_contact(contact_id)
        if c is None or not c.fp_id:
            return False
        state = d.fs.customer_state(c.fp_id)
        if state == c.fp_state:
            return False
        now = d.clock.now()
        previous, c.fp_state = c.fp_state, state
        c.tags.add(f"fp:{state.value}")
        d.store.save_contact(c, now)
        d.crm.set_stage(c.id, STAGE_FOR_STATE[state])
        d.crm.add_tags(c.id, {f"fp:{state.value}"})

        stopped = 0
        if state in STOPPING_STATES:
            stopped = d.store.cancel_enrollments(c.id, f"fp_state:{state.value}", now, STOPPABLE)
        if state.value == "paid":
            self.runner.enroll(c.id, "review_request",
                               {"link": f" {d.settings.review_link}" if d.settings.review_link else ""})
        d.metrics.inc("fp_state_changes_total", to=state.value)
        d.store.audit(now, "sync", "fp_state_changed", c.id, previous=previous.value, new=state.value, stopped=stopped)
        log(logger, logging.INFO, "fp state changed", contact=c.id, new=state.value, stopped=stopped)
        self.runner.run_due(c.id)
        return True

    def sync_by_fp_id(self, fp_id: str) -> bool:
        c = self.d.store.contact_by_fp(fp_id)
        return self.sync_contact(c.id) if c else False

    def sync_all(self) -> int:
        """Poll every non-final contact. Webhooks make this a safety net, not the hot path."""
        changed = 0
        for c in self.d.store.all_contacts():
            if c.fp_id and c.fp_state.value != "paid":
                try:
                    changed += self.sync_contact(c.id)
                except Exception:
                    logger.exception("sync failed for %s", c.id)
                    self.d.metrics.inc("sync_errors_total")
        return changed

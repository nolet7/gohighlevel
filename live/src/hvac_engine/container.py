"""Composition root: builds every service from Settings. The only place that knows about adapters."""
from __future__ import annotations

from dataclasses import dataclass

from .clock import SystemClock
from .compliance import Policy
from .config import Settings
from .metrics import Metrics
from .ports import Clock, CRMPort, FieldServicePort
from .services.campaigns import CampaignService
from .services.context import Deps
from .services.conversation import ConversationService, Qualifier
from .services.intake import IntakeService
from .services.runner import SequenceRunner
from .services.sync import SyncService
from .store import Store


@dataclass
class Engine:
    deps: Deps
    runner: SequenceRunner
    intake: IntakeService
    sync: SyncService
    conversation: ConversationService
    campaigns: CampaignService

    @property
    def store(self) -> Store:
        return self.deps.store

    @property
    def metrics(self) -> Metrics:
        return self.deps.metrics

    def tick(self) -> dict:
        """Periodic heartbeat: reconcile with FieldPulse, then fire any due sequence steps."""
        changed = self.sync.sync_all()
        sent = self.runner.run_due()
        return {"fp_changes": changed, "messages_sent": sent}


def build_engine(settings: Settings, *, crm: CRMPort | None = None, fs: FieldServicePort | None = None,
                 clock: Clock | None = None, store: Store | None = None,
                 qualifier: Qualifier | None = None) -> Engine:
    clock = clock or SystemClock()
    store = store or Store(settings.database_path)
    if crm is None or fs is None:
        if settings.mode == "live":
            from .adapters.fieldpulse import FieldPulseClient
            from .adapters.ghl import GHLClient
            crm = crm or GHLClient.from_settings(settings)
            fs = fs or FieldPulseClient.from_settings(settings, clock)
        else:
            from .adapters.mock import MockCRM, MockFieldPulse
            crm = crm or MockCRM()
            fs = fs or MockFieldPulse(clock, settings.timezone)
    deps = Deps(settings, store, crm, fs, clock, Policy(settings, store), Metrics())
    runner = SequenceRunner(deps)
    sync = SyncService(deps, runner)
    return Engine(
        deps=deps, runner=runner, sync=sync,
        intake=IntakeService(deps, runner),
        conversation=ConversationService(deps, runner, sync, qualifier),
        campaigns=CampaignService(deps, runner),
    )

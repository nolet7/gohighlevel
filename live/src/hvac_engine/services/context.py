from __future__ import annotations

from dataclasses import dataclass

from ..clock import SystemClock  # noqa: F401  (re-export convenience)
from ..compliance import Policy
from ..config import Settings
from ..metrics import Metrics
from ..ports import Clock, CRMPort, FieldServicePort
from ..store import Store


@dataclass
class Deps:
    settings: Settings
    store: Store
    crm: CRMPort
    fs: FieldServicePort
    clock: Clock
    policy: Policy
    metrics: Metrics

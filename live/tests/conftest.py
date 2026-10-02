from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hvac_engine.adapters.mock import MockCRM, MockFieldPulse
from hvac_engine.clock import FakeClock
from hvac_engine.config import Settings
from hvac_engine.container import build_engine
from hvac_engine.store import Store

TZ = ZoneInfo("America/New_York")


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 10, 2, 14, 40, tzinfo=TZ))   # Friday 2:40 PM Eastern


@pytest.fixture
def settings():
    return Settings(database_path=":memory:", webhook_secret="s3cret", admin_api_key="admin-key",
                    fieldpulse_webhook_token="fp-token")


@pytest.fixture
def crm():
    return MockCRM()


@pytest.fixture
def fs(clock):
    return MockFieldPulse(clock, "America/New_York")


@pytest.fixture
def engine(settings, crm, fs, clock):
    return build_engine(settings, crm=crm, fs=fs, clock=clock, store=Store(":memory:"))

from datetime import datetime

from hvac_engine.demo import main
from hvac_engine.scheduler import Scheduler
from tests.conftest import TZ
from tests.helpers import busy_calendar, seed_customers


def test_daily_jobs_run_once_per_day_after_their_time(engine, settings, clock):
    s = Scheduler(engine, settings)
    clock.set(datetime(2026, 10, 2, 8, 30, tzinfo=TZ))
    assert s.run_daily_due() == []
    clock.set(datetime(2026, 10, 2, 9, 5, tzinfo=TZ))
    assert s.run_daily_due() == ["fill_calendar"]
    assert s.run_daily_due() == []                                   # guard: not twice
    clock.set(datetime(2026, 10, 2, 11, 30, tzinfo=TZ))
    assert s.run_daily_due() == ["estimates", "database"]
    clock.set(datetime(2026, 10, 3, 9, 5, tzinfo=TZ))
    assert s.run_daily_due() == ["fill_calendar"]                    # next day runs again


def test_restart_in_the_afternoon_does_not_trigger_a_late_blast(engine, settings, clock):
    clock.set(datetime(2026, 10, 2, 15, 49, tzinfo=TZ))              # service restarted mid-afternoon
    assert Scheduler(engine, settings).run_daily_due() == []


def test_scheduled_campaigns_only_plan_until_campaigns_live(engine, settings, fs, clock, crm):
    seed_customers(fs, 60)
    busy_calendar(fs, clock)
    clock.set(datetime(2026, 10, 2, 9, 5, tzinfo=TZ))
    Scheduler(engine, settings).run_daily_due()
    assert crm.sent == []                                            # dry-run: nothing went out


def test_scheduled_campaigns_send_when_live(engine, settings, fs, clock, crm):
    settings.campaigns_live = True
    seed_customers(fs, 60)
    busy_calendar(fs, clock)
    clock.set(datetime(2026, 10, 2, 9, 5, tzinfo=TZ))
    Scheduler(engine, settings).run_daily_due()
    assert any("openings on" in m["text"] for m in crm.sent)


def test_demo_runs_clean(capsys):
    assert main() == 0
    out = capsys.readouterr().out
    assert "RESULT: 22/22 checks passed" in out

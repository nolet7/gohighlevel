"""In-process scheduler. Single-instance by design; for HA run jobs from external cron against
/admin/jobs/* (DB-level run guards make double-firing safe)."""
from __future__ import annotations

import asyncio
import logging

from .config import Settings
from .container import Engine

logger = logging.getLogger(__name__)

# (job name, local HH:MM). Campaigns only SEND when settings.campaigns_live is true; otherwise they plan.
DAILY = [("fill_calendar", "09:00"), ("estimates", "10:00"), ("database", "11:00")]   # after quiet hours end
# A job only fires within this window after its scheduled time, so a restart at 3 PM never triggers a late blast.
RUN_WINDOW_HOURS = 3


class Scheduler:
    def __init__(self, engine: Engine, settings: Settings):
        self.e, self.s = engine, settings
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def run_daily_due(self) -> list[str]:
        d = self.e.deps
        local = d.clock.now().astimezone(d.policy.tz)
        minutes, ran = local.hour * 60 + local.minute, []
        for name, at in DAILY:
            start = int(at[:2]) * 60 + int(at[3:])
            if start <= minutes < start + RUN_WINDOW_HOURS * 60 and \
                    d.store.first_time(f"cron:{name}:{local.date()}", d.clock.now()):
                dry = not self.s.campaigns_live
                c = self.e.campaigns
                {"fill_calendar": c.fill_calendar, "estimates": c.reactivate_estimates,
                 "database": c.reactivate_database}[name](dry_run=dry)
                ran.append(name)
        return ran

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.e.tick)
                await asyncio.to_thread(self.run_daily_due)
            except Exception:
                logger.exception("scheduler iteration failed")
            await asyncio.sleep(self.s.tick_seconds)

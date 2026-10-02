from __future__ import annotations

from datetime import UTC, datetime, timedelta


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """Deterministic clock for tests and the demo."""

    def __init__(self, start: datetime):
        if start.tzinfo is None:
            raise ValueError("FakeClock needs a timezone-aware datetime")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, dt: datetime) -> None:
        if dt.tzinfo is None:
            raise ValueError("timezone-aware datetime required")
        self._now = dt.astimezone(UTC)

    def advance(self, **kw: float) -> None:
        self._now += timedelta(**kw)

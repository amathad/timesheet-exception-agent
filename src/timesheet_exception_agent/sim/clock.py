"""Simulated clock for lock-day runs. Nothing waits in real time; the driver advances it.

All times are timezone-aware in America/Denver, matching the seeded pay periods.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Denver")

# Lock day for the period being worked in the seed (pay_periods.lock_at).
# START: coordinators begin. MANAGER_WINDOW: managers' final correction window opens.
# LOCK: managers can no longer change timesheets. END_OF_DAY: coordinators' last changes.
START = datetime(2026, 10, 26, 8, 0, tzinfo=TZ)
MANAGER_WINDOW = datetime(2026, 10, 26, 9, 0, tzinfo=TZ)
LOCK = datetime(2026, 10, 26, 10, 0, tzinfo=TZ)
END_OF_DAY = datetime(2026, 10, 26, 17, 0, tzinfo=TZ)

STEP = timedelta(minutes=10)


class SimClock:
    def __init__(self, start: datetime = START) -> None:
        self._now = _aware(start)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta = STEP) -> datetime:
        if delta < timedelta(0):
            raise ValueError("the clock cannot move backwards")
        self._now += delta
        return self._now

    def advance_to(self, when: datetime) -> datetime:
        return self.advance(_aware(when) - self._now)

    def minutes_until(self, when: datetime) -> float:
        """Minutes from now until `when`; negative once it has passed."""
        return (_aware(when) - self._now).total_seconds() / 60

    def is_past(self, when: datetime) -> bool:
        return self._now >= _aware(when)

    def __repr__(self) -> str:
        return f"SimClock({self._now:%a %Y-%m-%d %H:%M %Z})"


def _aware(when: datetime) -> datetime:
    if when.tzinfo is None:
        raise ValueError("use timezone-aware datetimes")
    return when.astimezone(TZ)


if __name__ == "__main__":
    clock = SimClock()
    while not clock.is_past(LOCK):
        print(f"{clock.now():%H:%M}  {clock.minutes_until(LOCK):>4.0f} min to lock")
        clock.advance()
    print(f"{clock.now():%H:%M}  locked")

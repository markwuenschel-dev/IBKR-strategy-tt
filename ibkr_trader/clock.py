"""Time as an injected dependency.

Nothing in this system calls ``datetime.now()`` or ``time.sleep()`` directly.
Time enters through a :class:`Clock`, which is what lets the mission test run
deterministically and never skip or flake near midnight.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """The only source of time in the application."""

    def now(self) -> datetime:
        """Current instant, timezone-aware and in UTC."""
        ...

    def sleep(self, seconds: float) -> None:
        """Block for ``seconds``."""
        ...


class SystemClock:
    """Real wall-clock time. Used in production."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FixedClock:
    """A clock that moves only when the test moves it.

    ``sleep`` advances the clock instead of blocking, so tests exercising the
    repeat-scan loop cost no wall-clock time.
    """

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware start instant")
        self._now = start
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        """Move time forward without recording a sleep."""
        self._now = self._now + timedelta(seconds=seconds)

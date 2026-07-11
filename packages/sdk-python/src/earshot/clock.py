"""Injectable nanosecond clocks used by recorders and deterministic tests."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol


class Clock(Protocol):
    def unix_nano(self) -> int: ...

    def monotonic_nano(self) -> int: ...


class SystemClock:
    def unix_nano(self) -> int:
        return time.time_ns()

    def monotonic_nano(self) -> int:
        return time.monotonic_ns()


@dataclass
class ManualClock:
    wall: int = 0
    monotonic: int = 0

    def unix_nano(self) -> int:
        return self.wall

    def monotonic_nano(self) -> int:
        return self.monotonic

    def advance(self, nanoseconds: int) -> None:
        if nanoseconds < 0:
            raise ValueError("manual monotonic clock cannot move backwards")
        self.wall += nanoseconds
        self.monotonic += nanoseconds

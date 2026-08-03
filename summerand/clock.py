"""Event-time clock and discrete-event scheduler.

Replay and live mode share this code. Nothing in the pipeline reads wall time: the clock only moves
when the driver releases an event or a watermark, and periodic jobs fire at their due event times in
timestamp order. Replay speed changes how long we sleep between events, never what happens.
"""

from __future__ import annotations

import heapq
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

SECOND_MS = 1_000
MINUTE_MS = 60 * SECOND_MS
HOUR_MS = 60 * MINUTE_MS

Job = Callable[[int], Awaitable[None] | None]


class Clock(Protocol):
    def now_ms(self) -> int: ...


class WallClock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000


class EventClock:
    """Logical clock. Unset until the first event is released; never moves backwards."""

    def __init__(self, start_ms: int | None = None) -> None:
        self._now = start_ms

    @property
    def started(self) -> bool:
        return self._now is not None

    def now_ms(self) -> int:
        if self._now is None:
            raise RuntimeError("event clock has not started")
        return self._now

    def set(self, ts_ms: int) -> None:
        if self._now is not None and ts_ms < self._now:
            raise ValueError(f"event clock cannot go backwards ({ts_ms} < {self._now})")
        self._now = ts_ms


@dataclass(order=True)
class _Due:
    due: int
    priority: int
    name: str
    period: int = field(compare=False)
    fn: Job = field(compare=False)


@dataclass
class _JobSpec:
    name: str
    period: int
    fn: Job
    priority: int


class Scheduler:
    """Fires periodic jobs at multiples of their period, in (due, priority, name) order."""

    def __init__(self, clock: EventClock) -> None:
        self.clock = clock
        self._specs: list[_JobSpec] = []
        self._heap: list[_Due] = []
        self.started = False
        self.fired: dict[str, int] = {}

    def every(self, name: str, period_ms: int, fn: Job, priority: int = 0) -> None:
        spec = _JobSpec(name, period_ms, fn, priority)
        self._specs.append(spec)
        if self.started:
            self._push_first(spec, self.clock.now_ms())

    def start(self, ts_ms: int) -> None:
        if self.started:
            return
        self.clock.set(ts_ms)
        self.started = True
        for spec in self._specs:
            self._push_first(spec, ts_ms)

    def _push_first(self, spec: _JobSpec, after_ms: int) -> None:
        due = (after_ms // spec.period + 1) * spec.period
        heapq.heappush(self._heap, _Due(due, spec.priority, spec.name, spec.period, spec.fn))

    def next_due(self) -> int | None:
        return self._heap[0].due if self._heap else None

    async def advance_to(self, ts_ms: int) -> None:
        """Run every job due at or before ts_ms, then leave the clock at ts_ms."""
        if not self.started:
            return
        while self._heap and self._heap[0].due <= ts_ms:
            item = heapq.heappop(self._heap)
            self.clock.set(item.due)
            result = item.fn(item.due)
            if inspect.isawaitable(result):
                await result
            self.fired[item.name] = self.fired.get(item.name, 0) + 1
            heapq.heappush(
                self._heap,
                _Due(item.due + item.period, item.priority, item.name, item.period, item.fn),
            )
        if ts_ms > self.clock.now_ms():
            self.clock.set(ts_ms)

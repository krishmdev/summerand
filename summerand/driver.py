"""Merge several input topics into one event-time ordered stream.

Each input topic keeps its own watermark: max event time seen minus the allowed lateness, or an
explicit `watermark` message from the producer. An event is released only once its timestamp is
below the minimum watermark over all inputs, so the release order is sorted by (ts, seq, topic)
no matter how the topics interleave on arrival. That is what makes an unpaced replay and a 300x
replay produce the same state. Events that arrive behind their topic's watermark are counted and
dropped.
"""

from __future__ import annotations

import heapq
import itertools
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable

from summerand.clock import MINUTE_MS, Scheduler
from summerand.schemas import Envelope

log = logging.getLogger(__name__)

Handler = Callable[[str, Envelope], Awaitable[None]]

_INF = float("inf")


class EventTimeDriver:
    def __init__(
        self,
        scheduler: Scheduler,
        handler: Handler,
        inputs: Iterable[str],
        lateness_ms: int = 2 * MINUTE_MS,
    ) -> None:
        self.scheduler = scheduler
        self.handler = handler
        self.inputs = list(inputs)
        self._order = {topic: i for i, topic in enumerate(self.inputs)}
        self.lateness_ms = lateness_ms
        self._heap: list[tuple[int, int, int, int, str, Envelope]] = []
        self._tiebreak = itertools.count()
        self._wm: dict[str, float] = {t: -_INF for t in self.inputs}
        self._max_seen: dict[str, int] = {}
        self._done: set[str] = set()
        self.late_dropped: Counter[str] = Counter()
        self.released = 0
        self.last_ts: int | None = None

    @property
    def finished(self) -> bool:
        return len(self._done) == len(self.inputs) and not self._heap

    def watermark(self) -> float:
        return min(self._wm.values()) if self._wm else -_INF

    def topic_watermark(self, topic: str) -> float:
        return self._wm[topic]

    async def offer(self, topic: str, env: Envelope) -> None:
        if topic not in self._wm:
            raise KeyError(f"unknown input topic {topic!r}")
        if env.kind == "eos":
            self._done.add(topic)
            self._wm[topic] = _INF
        elif env.kind == "watermark":
            self._wm[topic] = max(self._wm[topic], env.ts_ms)
        else:
            if env.ts_ms < self._wm[topic]:
                self.late_dropped[topic] += 1
                log.debug("dropped late %s on %s (ts=%s)", env.kind, topic, env.ts_ms)
                return
            heapq.heappush(
                self._heap,
                (env.ts_ms, env.seq, self._order[topic], next(self._tiebreak), topic, env),
            )
            seen = max(self._max_seen.get(topic, env.ts_ms), env.ts_ms)
            self._max_seen[topic] = seen
            self._wm[topic] = max(self._wm[topic], seen - self.lateness_ms)
        await self._release()

    async def advance_idle(self, topic: str, ts_ms: int) -> None:
        """Live mode: move an idle topic's watermark forward from wall time."""
        if topic in self._done:
            return
        self._wm[topic] = max(self._wm[topic], ts_ms)
        await self._release()

    async def _release(self) -> None:
        low = self.watermark()
        all_done = len(self._done) == len(self.inputs)
        while self._heap and (all_done or self._heap[0][0] < low):
            ts, _, _, _, topic, env = heapq.heappop(self._heap)
            if not self.scheduler.started:
                self.scheduler.start(ts)
            await self.scheduler.advance_to(ts)
            self.last_ts = ts
            self.released += 1
            await self.handler(topic, env)
        if all_done:
            if self.last_ts is not None:
                await self.scheduler.advance_to(self.last_ts)
        elif low != -_INF and self.scheduler.started:
            await self.scheduler.advance_to(int(low))

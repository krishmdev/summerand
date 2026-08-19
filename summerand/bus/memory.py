"""In-process bus for the demo and tests. Every subscription gets its own queue (fan-out)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

_CLOSED = object()


class MemorySubscription:
    def __init__(self, bus: InMemoryBus, topic: str, maxsize: int) -> None:
        self._bus = bus
        self.topic = topic
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self._closed = False

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            item = await self.queue.get()
            if item is _CLOSED:
                return
            yield item

    async def caught_up(self) -> bool:
        return self.queue.empty()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._bus._unsubscribe(self)
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(_CLOSED)


class InMemoryBus:
    def __init__(self, maxsize: int = 50_000) -> None:
        self._subs: dict[str, list[MemorySubscription]] = defaultdict(list)
        self._maxsize = maxsize
        self.published: dict[str, int] = defaultdict(int)

    async def publish(self, topic: str, msg: dict[str, Any], key: str | None = None) -> None:
        # Round-trip through JSON so the demo behaves like Kafka: no shared mutable objects and
        # no values that would fail to serialize in compose mode.
        payload = json.dumps(msg, separators=(",", ":"))
        self.published[topic] += 1
        for sub in list(self._subs[topic]):
            await sub.queue.put(json.loads(payload))

    def subscribe(
        self, topic: str, group: str | None = None, *, from_beginning: bool = True
    ) -> MemorySubscription:
        sub = MemorySubscription(self, topic, self._maxsize)
        self._subs[topic].append(sub)
        return sub

    def _unsubscribe(self, sub: MemorySubscription) -> None:
        if sub in self._subs[sub.topic]:
            self._subs[sub.topic].remove(sub)

    async def close(self) -> None:
        for subs in list(self._subs.values()):
            for sub in list(subs):
                await sub.close()

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol


class Subscription(Protocol):
    def __aiter__(self) -> AsyncIterator[dict[str, Any]]: ...

    async def close(self) -> None: ...


class Bus(Protocol):
    """Topic-based pub/sub carrying JSON-compatible dicts (usually `Envelope.model_dump()`)."""

    async def publish(self, topic: str, msg: dict[str, Any], key: str | None = None) -> None: ...

    def subscribe(
        self, topic: str, group: str | None = None, *, from_beginning: bool = True
    ) -> Subscription: ...

    async def close(self) -> None: ...

"""WebSocket fan-out. Each client gets a bounded queue; a client that falls 100 messages behind is
disconnected rather than allowed to grow memory or slow everyone else down."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from summerand.bus import Bus
from summerand.bus import topics as T
from summerand.schemas import Envelope

log = logging.getLogger(__name__)

QUEUE_SIZE = 100
HEARTBEAT_S = 15.0


@dataclass(eq=False)
class Client:
    ws: WebSocket
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))
    tickers: set[str] = field(default_factory=set)
    dropped: bool = False


def filter_for(msg: dict[str, Any], tickers: set[str]) -> dict[str, Any] | None:
    if not tickers:
        return msg
    if msg["type"] == "snapshot":
        stories = [s for s in msg.get("stories", []) if tickers & set(s.get("tickers", []))]
        return {**msg, "stories": stories, "filter": sorted(tickers)}
    if msg["type"] == "cluster_update":
        story = msg.get("story") or {}
        return msg if tickers & set(story.get("tickers", [])) else None
    return msg


class Hub:
    def __init__(self, event_clock: Any = None) -> None:
        self.clients: set[Client] = set()
        self.latest: dict[str, dict[str, Any]] = {}
        self.dropped_clients = 0
        self._event_clock = event_clock
        # Used before any snapshot has come through the bus (e.g. the API restarted).
        self.fallback_snapshot: Callable[[], dict[str, Any] | None] | None = None

    def broadcast(self, msg: dict[str, Any]) -> None:
        if msg["type"] in ("snapshot", "market_brief"):
            self.latest[msg["type"]] = msg
        for client in list(self.clients):
            out = filter_for(msg, client.tickers)
            if out is None:
                continue
            try:
                client.queue.put_nowait(out)
            except asyncio.QueueFull:
                client.dropped = True
                self.clients.discard(client)
                self.dropped_clients += 1
                log.warning("dropping slow websocket client")
                with contextlib.suppress(asyncio.QueueFull):
                    client.queue.put_nowait({"type": "_close"})

    def on_envelope(self, env: Envelope) -> None:
        if env.kind == "snapshot":
            self.broadcast({**env.data, "type": "snapshot"})
        elif env.kind == "cluster_update":
            self.broadcast({"type": "cluster_update", "ts_ms": env.ts_ms, **env.data})
        elif env.kind == "market_brief":
            self.broadcast({"type": "market_brief", **env.data})

    def consume(
        self, bus: Bus, group: str | None = None, from_beginning: bool = True
    ) -> Coroutine[Any, Any, None]:
        """Subscribe now (so nothing published after this call is missed) and return the loop."""
        subs = [
            bus.subscribe(t, group, from_beginning=from_beginning)
            for t in (T.RANKED_STORIES, T.BRIEFS)
        ]

        async def one(sub: Any) -> None:
            async for msg in sub:
                self.on_envelope(Envelope.model_validate(msg))

        async def run() -> None:
            await asyncio.gather(*(one(s) for s in subs))

        return run()

    async def heartbeat(self, interval: float = HEARTBEAT_S) -> None:
        while True:
            await asyncio.sleep(interval)
            event_ms = None
            if self._event_clock is not None and self._event_clock.started:
                event_ms = self._event_clock.now_ms()
            self.broadcast(
                {"type": "heartbeat", "ts_ms": event_ms, "wall_ms": time.time_ns() // 1_000_000}
            )

    async def serve(self, ws: WebSocket, initial: list[dict[str, Any]]) -> None:
        await ws.accept()
        client = Client(ws)
        for msg in initial:
            client.queue.put_nowait(msg)
        self.clients.add(client)

        async def sender() -> None:
            while True:
                msg = await client.queue.get()
                if msg["type"] == "_close":
                    await ws.close(code=1013, reason="client too slow")
                    return
                await ws.send_json(msg)

        async def receiver() -> None:
            while True:
                msg = await ws.receive_json()
                if isinstance(msg, dict) and msg.get("type") == "subscribe":
                    client.tickers = {str(t).upper() for t in msg.get("tickers") or []}
                    snap = self.latest.get("snapshot")
                    if snap is None and self.fallback_snapshot is not None:
                        snap = self.fallback_snapshot()
                    if snap is not None:
                        snap = {**snap, "type": "snapshot"}
                        out = filter_for(snap, client.tickers)
                        if out is not None:
                            with contextlib.suppress(asyncio.QueueFull):
                                client.queue.put_nowait(out)

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except WebSocketDisconnect:
            pass
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                    await t
            self.clients.discard(client)

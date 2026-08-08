"""Polygon stocks trade stream -> ticks_equity.

Fixes from sprint-1: trade `t` is Unix milliseconds (it was divided by 1e9, which put every trade
in January 1970); the stream now waits for `auth_success` instead of ignoring status messages, and
reconnects with backoff. Polygon's free plan has no stocks WebSocket, so this service is optional.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import Callable
from typing import Any

import websockets

from summerand.bus import Bus
from summerand.bus import topics as T
from summerand.schemas import Envelope, Tick

log = logging.getLogger(__name__)

DEFAULT_SYMBOLS = ["AAPL", "MSFT", "NVDA", "META", "COIN"]


class PolygonAuthError(RuntimeError):
    pass


def parse_polygon(raw: str | bytes) -> tuple[list[Tick], list[dict[str, Any]]]:
    """Split one socket frame into trades and status messages."""
    ticks: list[Tick] = []
    statuses: list[dict[str, Any]] = []
    for msg in json.loads(raw):
        ev = msg.get("ev")
        if ev == "status":
            statuses.append(msg)
        elif ev == "T":
            ticks.append(
                Tick(
                    symbol=msg["sym"],
                    asset_class="equity",
                    price=float(msg["p"]),
                    size=float(msg.get("s", 0)),
                    ts_ms=int(msg["t"]),
                    venue="polygon",
                )
            )
    return ticks, statuses


async def _await_status(ws: Any, wanted: str) -> None:
    while True:
        _, statuses = parse_polygon(await ws.recv())
        for status in statuses:
            if status.get("status") == wanted:
                return
            if status.get("status") in ("auth_failed", "error"):
                raise PolygonAuthError(status.get("message", "authentication failed"))


async def stream_polygon(
    bus: Bus,
    api_key: str,
    url: str,
    symbols: list[str] | None = None,
    connect: Callable[..., Any] = websockets.connect,
    max_backoff: float = 60.0,
) -> None:
    symbols = symbols or DEFAULT_SYMBOLS
    seq = itertools.count()
    backoff = 1.0
    while True:
        try:
            async with connect(url, ping_interval=15) as ws:
                await _await_status(ws, "connected")
                await ws.send(json.dumps({"action": "auth", "params": api_key}))
                await _await_status(ws, "auth_success")
                await ws.send(
                    json.dumps(
                        {"action": "subscribe", "params": ",".join(f"T.{s}" for s in symbols)}
                    )
                )
                log.info("polygon subscribed to %s", ",".join(symbols))
                backoff = 1.0
                async for raw in ws:
                    ticks, statuses = parse_polygon(raw)
                    for status in statuses:
                        log.info("polygon status: %s", status.get("message"))
                    for tick in ticks:
                        env = Envelope(
                            kind="tick", ts_ms=tick.ts_ms, seq=next(seq), data=tick.model_dump()
                        )
                        await bus.publish(T.TICKS_EQUITY, env.model_dump(), key=tick.symbol)
        except PolygonAuthError:
            raise
        except (OSError, websockets.WebSocketException) as exc:
            log.warning("polygon stream dropped (%s); reconnecting in %.0fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

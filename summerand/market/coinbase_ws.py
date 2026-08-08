"""Coinbase Exchange public market data: live ticker WebSocket and REST 1-minute candles.

Neither needs a key. The WebSocket feeds ticks_crypto in live/compose mode; the candles endpoint is
what scripts/record_fixture.py uses to backfill the demo fixture.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import websockets

from summerand.bus import Bus
from summerand.bus import topics as T
from summerand.schemas import MINUTE_MS, Bar, Envelope, Tick, iso_to_ms, ms_to_iso

log = logging.getLogger(__name__)

REST_URL = "https://api.exchange.coinbase.com"
DEFAULT_PRODUCTS = ["BTC-USD", "ETH-USD", "SOL-USD"]


def product_symbol(product_id: str) -> str:
    return product_id.split("-")[0]


def parse_coinbase(msg: dict[str, Any]) -> Tick | None:
    if msg.get("type") != "ticker" or "price" not in msg or "time" not in msg:
        return None
    return Tick(
        symbol=product_symbol(msg["product_id"]),
        asset_class="crypto",
        price=float(msg["price"]),
        size=float(msg.get("last_size") or 0),
        ts_ms=iso_to_ms(msg["time"]),
        venue="coinbase",
    )


async def stream_coinbase(
    bus: Bus,
    url: str,
    products: list[str] | None = None,
    connect: Callable[..., Any] = websockets.connect,
    max_backoff: float = 60.0,
) -> None:
    products = products or DEFAULT_PRODUCTS
    seq = itertools.count()
    backoff = 1.0
    while True:
        try:
            async with connect(url, ping_interval=20) as ws:
                await ws.send(
                    json.dumps(
                        {"type": "subscribe", "product_ids": products, "channels": ["ticker"]}
                    )
                )
                log.info("coinbase subscribed to %s", ",".join(products))
                backoff = 1.0
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") == "error":
                        log.warning("coinbase error: %s", msg.get("message"))
                        continue
                    tick = parse_coinbase(msg)
                    if tick is None:
                        continue
                    env = Envelope(
                        kind="tick", ts_ms=tick.ts_ms, seq=next(seq), data=tick.model_dump()
                    )
                    await bus.publish(T.TICKS_CRYPTO, env.model_dump(), key=tick.symbol)
        except (OSError, websockets.WebSocketException) as exc:
            log.warning("coinbase stream dropped (%s); reconnecting in %.0fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)


async def fetch_candles(
    client: httpx.AsyncClient, product: str, start_ms: int, end_ms: int
) -> list[Bar]:
    """1-minute candles in [start_ms, end_ms), 300 per request (the endpoint's limit)."""
    bars: dict[int, Bar] = {}
    step = 300 * MINUTE_MS
    cursor = start_ms - start_ms % MINUTE_MS
    while cursor < end_ms:
        stop = min(cursor + step, end_ms)
        resp = await client.get(
            f"{REST_URL}/products/{product}/candles",
            params={
                "granularity": 60,
                "start": ms_to_iso(cursor),
                "end": ms_to_iso(stop - MINUTE_MS),
            },
            timeout=20,
        )
        resp.raise_for_status()
        for t, low, high, open_, close, volume in resp.json():
            ts = int(t) * 1000
            if start_ms <= ts < end_ms:
                bars[ts] = Bar(
                    symbol=product_symbol(product),
                    ts_ms=ts,
                    open=float(open_),
                    high=float(high),
                    low=float(low),
                    close=float(close),
                    volume=float(volume),
                    venue="coinbase",
                )
        cursor = stop
        await asyncio.sleep(0.25)  # public endpoint allows ~10 req/s; stay well under
    return [bars[k] for k in sorted(bars)]

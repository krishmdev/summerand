"""Publish a recorded fixture onto the bus in timestamp order.

The fixture is JSONL (optionally gzipped), sorted by event time:
    {"type": "news", "ts_ms": ..., "source", "title", "url", "summary", "published_ms"}
    {"type": "bar",  "ts_ms": <bar end>, "symbol", "start_ms", "o", "h", "l", "c", "v", "venue"}

`speed` only sets how long to sleep between records (300 means 24h of fixture in under 5 minutes);
None publishes as fast as possible. The line number is the seq, and a watermark is sent on each
topic every minute of fixture time, so downstream results don't depend on speed.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from summerand.bus import Bus
from summerand.bus import topics as T
from summerand.etl.dedup import article_id
from summerand.schemas import Bar, Envelope, RawNews

HEARTBEAT_MS = 60_000


def read_fixture(path: Path) -> Iterator[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def to_envelope(rec: dict[str, Any], seq: int) -> tuple[str, Envelope]:
    if rec["type"] == "news":
        raw = RawNews(
            id=article_id(rec["url"]),
            source=rec["source"],
            title=rec["title"],
            url=rec["url"],
            summary=rec.get("summary", ""),
            published_ms=rec.get("published_ms", rec["ts_ms"]),
            observed_ms=rec["ts_ms"],
        )
        return T.RAW_NEWS, Envelope(kind="news", ts_ms=rec["ts_ms"], seq=seq, data=raw.model_dump())
    bar = Bar(
        symbol=rec["symbol"],
        ts_ms=rec["start_ms"],
        open=rec["o"],
        high=rec["h"],
        low=rec["l"],
        close=rec["c"],
        volume=rec["v"],
        venue=rec.get("venue", "fixture"),
    )
    return T.BARS, Envelope(kind="bar", ts_ms=rec["ts_ms"], seq=seq, data=bar.model_dump())


async def replay(
    bus: Bus,
    path: Path,
    speed: float | None = None,
    topics: tuple[str, ...] = (T.RAW_NEWS, T.BARS),
    progress: dict[str, Any] | None = None,
    until_ms: int | None = None,
) -> int:
    records = [r for r in read_fixture(path) if until_ms is None or r["ts_ms"] <= until_ms]
    if progress is not None:
        progress.update(
            total=len(records),
            sent=0,
            first_ms=records[0]["ts_ms"] if records else None,
            last_ms=records[-1]["ts_ms"] if records else None,
        )
    wall0 = time.monotonic()
    ts0 = records[0]["ts_ms"] if records else 0
    next_hb = (ts0 // HEARTBEAT_MS + 1) * HEARTBEAT_MS
    for seq, rec in enumerate(records):
        ts = rec["ts_ms"]
        if speed:
            delay = wall0 + (ts - ts0) / 1000 / speed - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        while next_hb <= ts:
            for topic in topics:
                await bus.publish(topic, Envelope(kind="watermark", ts_ms=next_hb).model_dump())
            next_hb += HEARTBEAT_MS
        topic, env = to_envelope(rec, seq)
        await bus.publish(topic, env.model_dump())
        if progress is not None:
            progress.update(sent=seq + 1, event_ms=ts)
        if not speed and seq % 200 == 0:
            await asyncio.sleep(0)  # let the API breathe during an unpaced replay
    for topic in topics:
        await bus.publish(
            topic, Envelope(kind="eos", ts_ms=records[-1]["ts_ms"] if records else 0).model_dump()
        )
    return len(records)

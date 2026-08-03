"""Message and record types shared by every stage. All timestamps are UTC epoch milliseconds."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

MINUTE_MS = 60_000


def ms_to_iso(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts_ms % 1000:03d}Z"


def iso_to_ms(value: str) -> int:
    """Parse an ISO-8601 timestamp. Naive values are treated as UTC, never local time."""
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return round(dt.timestamp() * 1000)


class Envelope(BaseModel):
    """What travels on the bus. `ts_ms` is event time; `seq` breaks ties deterministically.

    kind is one of: news, bar, tick, watermark, eos, snapshot, cluster_update, market_brief.
    A `watermark` promises that no later message on the same topic has ts_ms below it.
    """

    kind: str
    ts_ms: int
    seq: int = 0
    data: dict[str, Any] = Field(default_factory=dict)


class RawNews(BaseModel):
    id: str
    source: str
    title: str
    url: str
    summary: str = ""
    published_ms: int
    observed_ms: int
    ts_inferred: bool = False


class CleanNews(BaseModel):
    id: str
    source: str
    title: str
    url: str
    summary: str
    published_ms: int
    observed_ms: int
    tickers: list[str] = Field(default_factory=list)
    simhash: str = ""

    @property
    def text(self) -> str:
        if self.summary:
            return f"{self.title}. {self.summary}"
        return self.title


class Tick(BaseModel):
    symbol: str
    asset_class: str
    price: float
    size: float
    ts_ms: int
    venue: str


class Bar(BaseModel):
    """One-minute OHLCV bar. `ts_ms` is the minute start; the bar is known at `end_ms`."""

    symbol: str
    ts_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    venue: str

    @property
    def end_ms(self) -> int:
        return self.ts_ms + MINUTE_MS

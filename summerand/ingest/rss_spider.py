"""Poll RSS feeds and publish new items to raw_news.

Each item's id is sha256 of its canonical URL and its publish time comes from the feed. Only items
not seen before on that feed are published, with their summaries, and conditional GETs (ETag /
Last-Modified) skip unchanged feeds. A feed that errors is backed off exponentially.
"""

from __future__ import annotations

import asyncio
import calendar
import itertools
import logging
from collections import OrderedDict
from dataclasses import dataclass

import feedparser
import httpx

from summerand.bus import Bus
from summerand.bus import topics as T
from summerand.clock import Clock, WallClock
from summerand.config import FeedConfig
from summerand.etl.dedup import article_id
from summerand.schemas import Envelope, RawNews

log = logging.getLogger(__name__)

USER_AGENT = "SummerandRSS/0.2 (+https://github.com/krishmdev/summerand)"


def entry_published_ms(entry: feedparser.FeedParserDict) -> int | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            # feedparser normalizes to a UTC struct_time; timegm (not mktime) keeps it in UTC.
            return calendar.timegm(parsed) * 1000
    return None


def parse_feed(content: bytes | str, feed: FeedConfig, observed_ms: int) -> list[RawNews]:
    parsed = feedparser.parse(content)
    items = []
    for entry in parsed.entries:
        link = entry.get("link")
        title = entry.get("title")
        if not link or not title:
            continue
        published = entry_published_ms(entry)
        summary = entry.get("summary") or entry.get("description") or ""
        items.append(
            RawNews(
                id=article_id(link),
                source=feed.name,
                title=title,
                url=link,
                summary=summary,
                published_ms=published if published is not None else observed_ms,
                observed_ms=observed_ms,
                ts_inferred=published is None,
            )
        )
    return items


@dataclass
class _FeedState:
    etag: str | None = None
    last_modified: str | None = None
    failures: int = 0
    skip_polls: int = 0


class RssSpider:
    def __init__(
        self,
        feeds: list[FeedConfig],
        client: httpx.AsyncClient,
        clock: Clock | None = None,
        seen_per_feed: int = 2000,
    ) -> None:
        self.feeds = [f for f in feeds if f.enabled]
        self.client = client
        self.clock = clock or WallClock()
        self._seen: dict[str, OrderedDict[str, None]] = {f.name: OrderedDict() for f in self.feeds}
        self._state = {f.name: _FeedState() for f in self.feeds}
        self._cap = seen_per_feed

    def _is_new(self, feed: str, item_id: str) -> bool:
        seen = self._seen[feed]
        if item_id in seen:
            seen.move_to_end(item_id)
            return False
        seen[item_id] = None
        if len(seen) > self._cap:
            seen.popitem(last=False)
        return True

    async def fetch(self, feed: FeedConfig) -> list[RawNews]:
        state = self._state[feed.name]
        if state.skip_polls > 0:
            state.skip_polls -= 1
            return []
        headers = {"User-Agent": USER_AGENT}
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified
        try:
            resp = await self.client.get(feed.url, headers=headers, timeout=15)
            if resp.status_code == 304:
                return []
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            state.failures += 1
            state.skip_polls = min(2**state.failures, 32) - 1
            log.warning("feed %s failed (%s); skipping %d polls", feed.name, exc, state.skip_polls)
            return []
        state.failures = 0
        state.etag = resp.headers.get("etag")
        state.last_modified = resp.headers.get("last-modified")
        items = parse_feed(resp.content, feed, self.clock.now_ms())
        return [it for it in items if self._is_new(feed.name, it.id)]

    async def poll_once(self) -> list[RawNews]:
        batches = await asyncio.gather(*(self.fetch(f) for f in self.feeds))
        return [item for batch in batches for item in batch]


# Watermarks trail the poll by a minute so an item from another raw_news producer (CryptoPanic)
# observed just before our poll ended isn't behind them.
WATERMARK_MARGIN_MS = 60_000


async def run_rss(bus: Bus, spider: RssSpider, poll_seconds: int) -> None:
    """Publish new items, then a watermark. The pipeline advances clean_news only from these
    watermarks (forwarded by the ETL), never from wall time, so a stalled ETL delays news
    instead of turning its backlog into late events."""
    seq = itertools.count()
    while True:
        items = await spider.poll_once()
        for item in sorted(items, key=lambda x: (x.published_ms, x.id)):
            env = Envelope(
                kind="news", ts_ms=item.observed_ms, seq=next(seq), data=item.model_dump()
            )
            await bus.publish(T.RAW_NEWS, env.model_dump(), key=item.id)
        mark = spider.clock.now_ms() - WATERMARK_MARGIN_MS
        await bus.publish(T.RAW_NEWS, Envelope(kind="watermark", ts_ms=mark).model_dump())
        if items:
            log.info("published %d new items", len(items))
        await asyncio.sleep(poll_seconds)

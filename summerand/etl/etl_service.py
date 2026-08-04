"""raw_news -> clean_news: strip HTML, canonicalize URLs, drop duplicates, extract tickers."""

from __future__ import annotations

import html
import logging
import re
import warnings

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

from summerand.bus import Bus
from summerand.bus import topics as T
from summerand.config import Watchlist
from summerand.etl.dedup import (
    BloomFilter,
    NearDuplicateIndex,
    article_id,
    canonical_url,
    simhash64,
)
from summerand.etl.tickers import TickerExtractor
from summerand.schemas import CleanNews, Envelope, RawNews

log = logging.getLogger(__name__)

warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)

SUMMARY_CHARS = 300
_WS = re.compile(r"\s+")
_BOILERPLATE = re.compile(
    r"(The post .{0,200}? appeared first on .{0,80}?\.?$)|(Continue reading.*$)|(Read more.*$)",
    re.IGNORECASE,
)


def strip_html(text: str) -> str:
    if not text:
        return ""
    if "<" in text or "&" in text:
        text = BeautifulSoup(text, "lxml").get_text(" ", strip=True)
    return _WS.sub(" ", html.unescape(text)).strip()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut + "…"


def normalize(raw: RawNews, tickers: TickerExtractor) -> CleanNews | None:
    title = strip_html(raw.title)
    if not title:
        return None
    summary = _BOILERPLATE.sub("", strip_html(raw.summary)).strip()
    if summary.lower().startswith(title.lower()):
        summary = summary[len(title) :].lstrip(" .:-")
    summary = truncate(summary, SUMMARY_CHARS)
    url = canonical_url(raw.url)
    return CleanNews(
        id=article_id(raw.url),
        source=raw.source,
        title=title,
        url=url,
        summary=summary,
        # An item can't be published after we saw it; clamp feeds with skewed or future dates.
        published_ms=min(raw.published_ms, raw.observed_ms),
        observed_ms=raw.observed_ms,
        tickers=tickers.extract(f"{title} {summary}"),
        simhash=f"{simhash64(title):016x}",
    )


class Etl:
    def __init__(self, watchlist: Watchlist) -> None:
        self.tickers = TickerExtractor(watchlist)
        self.seen = BloomFilter()
        self.near = NearDuplicateIndex()
        self.dropped_exact = 0
        self.dropped_near = 0

    def process(self, raw: RawNews) -> CleanNews | None:
        item = normalize(raw, self.tickers)
        if item is None:
            return None
        if item.id in self.seen:
            self.dropped_exact += 1
            return None
        self.seen.add(item.id)
        h = int(item.simhash, 16)
        dup_of = self.near.find(h)
        if dup_of is not None:
            self.dropped_near += 1
            log.debug("near-duplicate %r of %s", item.title, dup_of)
            return None
        self.near.add(h, item.id)
        return item


async def run_etl(bus: Bus, etl: Etl, group: str = "etl") -> None:
    """Consume raw_news until end-of-stream, forwarding watermarks so event time keeps moving."""
    sub = bus.subscribe(T.RAW_NEWS, group)
    async for msg in sub:
        env = Envelope.model_validate(msg)
        if env.kind in ("watermark", "eos"):
            await bus.publish(T.CLEAN_NEWS, env.model_dump())
            if env.kind == "eos":
                break
            continue
        if env.kind != "news":
            continue
        item = etl.process(RawNews.model_validate(env.data))
        if item is None:
            continue
        out = Envelope(kind="news", ts_ms=env.ts_ms, seq=env.seq, data=item.model_dump())
        await bus.publish(T.CLEAN_NEWS, out.model_dump(), key=item.id)
    await sub.close()

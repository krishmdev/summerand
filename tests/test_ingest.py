import httpx

from summerand.config import FeedConfig, load_sources
from summerand.ingest.rss_spider import RssSpider
from tests.conftest import ROOT
from tests.test_timestamps import RSS


class FixedClock:
    def now_ms(self):
        return 1_800_000_000_000


async def test_spider_sends_only_new_items_and_uses_conditional_get():
    calls = []

    def handler(request):
        calls.append(dict(request.headers))
        if request.headers.get("if-none-match") == '"v1"' and len(calls) == 2:
            return httpx.Response(304)
        return httpx.Response(200, content=RSS.encode(), headers={"etag": '"v1"'})

    feeds = [FeedConfig("ex", "https://example.com/rss", 0.5),
             FeedConfig("dead", "https://example.com/dead", 0.5, enabled=False)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        spider = RssSpider(feeds, client, clock=FixedClock())
        first = await spider.poll_once()
        second = await spider.poll_once()  # 304
        third = await spider.poll_once()  # same items again
    assert len(first) == 2 and second == [] and third == []
    assert len(calls) == 3  # the disabled feed is never fetched
    assert calls[1]["if-none-match"] == '"v1"'


async def test_failing_feed_backs_off():
    def handler(request):
        return httpx.Response(403)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        spider = RssSpider([FeedConfig("blocked", "https://example.com/x", 0.5)], client)
        for _ in range(4):
            assert await spider.poll_once() == []
    assert spider._state["blocked"].failures == 2  # polls 2 and 4 were skipped


def test_sources_config():
    cfg = load_sources(ROOT / "config/sources.yaml")
    names = [f.name for f in cfg.enabled_feeds]
    assert "reuters" not in names and "theblock" in names
    assert "{token}" in cfg.cryptopanic_url
    assert cfg.credibility("fed") == 0.95
    assert cfg.credibility("unknown-blog") == cfg.default_credibility

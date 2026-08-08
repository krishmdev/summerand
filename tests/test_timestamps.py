import time

from summerand.config import FeedConfig
from summerand.ingest.cryptopanic import parse_posts
from summerand.ingest.rss_spider import parse_feed
from summerand.market.coinbase_ws import parse_coinbase
from summerand.market.polygon_ws import parse_polygon
from summerand.schemas import iso_to_ms, ms_to_iso


def test_polygon_trade_t_is_milliseconds():
    ticks, statuses = parse_polygon(
        '[{"ev":"status","status":"auth_success","message":"authenticated"},'
        '{"ev":"T","sym":"AAPL","p":210.5,"s":100,"t":1719500000123}]'
    )
    assert statuses[0]["status"] == "auth_success"
    assert ms_to_iso(ticks[0].ts_ms) == "2024-06-27T14:53:20.123Z"


def test_cryptopanic_timestamp_is_utc_regardless_of_local_tz(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        items = parse_posts(
            {
                "results": [
                    {
                        "id": 1,
                        "title": "t",
                        "url": "https://x.io/a",
                        "published_at": "2024-06-27T14:53:20Z",
                    }
                ]
            },
            observed_ms=0,
        )
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    assert ms_to_iso(items[0].published_ms) == "2024-06-27T14:53:20.000Z"


def test_naive_iso_is_read_as_utc():
    assert iso_to_ms("2024-06-27T14:53:20") == iso_to_ms("2024-06-27T14:53:20+00:00")


RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
<item><title>Bitcoin &amp; ETH rally</title><link>https://www.example.com/a?utm_source=rss</link>
<pubDate>Thu, 27 Jun 2024 10:53:20 -0400</pubDate><description>&lt;p&gt;Body text&lt;/p&gt;</description></item>
<item><title>No date</title><link>https://example.com/b</link></item>
</channel></rss>"""


def test_rss_uses_publish_time_and_keeps_summary():
    feed = FeedConfig("ex", "https://example.com/rss", 0.5)
    a, b = parse_feed(RSS, feed, observed_ms=1_800_000_000_000)
    assert ms_to_iso(a.published_ms) == "2024-06-27T14:53:20.000Z"
    assert not a.ts_inferred and "Body text" in a.summary
    assert b.ts_inferred and b.published_ms == 1_800_000_000_000


def test_coinbase_ticker():
    tick = parse_coinbase(
        {
            "type": "ticker",
            "product_id": "BTC-USD",
            "price": "64000.01",
            "last_size": "0.01",
            "time": "2024-06-27T14:53:20.123456Z",
        }
    )
    assert tick.symbol == "BTC" and tick.ts_ms == 1719500000123
    assert parse_coinbase({"type": "subscriptions"}) is None

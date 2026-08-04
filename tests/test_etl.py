from summerand.bus import InMemoryBus
from summerand.bus import topics as T
from summerand.etl.dedup import BloomFilter, article_id, canonical_url, hamming, simhash64
from summerand.etl.etl_service import Etl, run_etl, strip_html
from summerand.schemas import Envelope, RawNews


def raw(url, title, summary="", ts=1_000):
    return RawNews(
        id=article_id(url),
        source="coindesk",
        title=title,
        url=url,
        summary=summary,
        published_ms=ts,
        observed_ms=ts,
    )


def test_strip_html():
    assert strip_html("<p>Hello&nbsp;<b>world</b></p>\n\n") == "Hello world"


def test_canonical_url_strips_tracking_and_www():
    a = canonical_url("http://www.Example.com/story/?utm_source=x&b=2&a=1#top")
    assert a == "https://example.com/story?a=1&b=2"
    assert article_id("https://example.com/story?b=2&a=1&fbclid=z") == article_id(a)


def test_bloom_filter_is_deterministic():
    bf = BloomFilter(capacity=1000, error_rate=0.01)
    bf.add("abc")
    assert "abc" in bf and "abd" not in bf


def test_simhash_near_duplicates():
    a = simhash64("Bitcoin falls below $60,000 as ETF outflows grow - CoinDesk")
    b = simhash64("Bitcoin falls below $60,000 as ETF outflows grow")
    c = simhash64("Nvidia beats estimates on data center demand")
    assert hamming(a, b) <= 3 < hamming(a, c)


def test_etl_drops_exact_and_near_duplicates(watchlist):
    etl = Etl(watchlist)
    out = [
        etl.process(
            raw("https://x.com/a?utm_medium=rss", "Solana outage halts blocks", "<p>Validators</p>")
        ),
        etl.process(raw("https://x.com/a", "Solana outage halts blocks")),
        etl.process(raw("https://y.com/other", "Solana outage halts blocks - Decrypt")),
        etl.process(raw("https://y.com/z", "Nvidia beats on data center demand")),
    ]
    assert [o is not None for o in out] == [True, False, False, True]
    assert out[0].summary == "Validators" and out[0].tickers == ["SOL"]
    assert etl.dropped_exact == 1 and etl.dropped_near == 1


def test_published_time_is_clamped_to_observed(watchlist):
    item = Etl(watchlist).process(
        RawNews(
            id="x",
            source="s",
            title="Future dated",
            url="https://a.b/c",
            published_ms=5_000,
            observed_ms=2_000,
        )
    )
    assert item.published_ms == 2_000


async def test_run_etl_forwards_watermarks(watchlist):
    bus = InMemoryBus()
    out = bus.subscribe(T.CLEAN_NEWS)
    import asyncio

    task = asyncio.create_task(run_etl(bus, Etl(watchlist)))
    await asyncio.sleep(0)
    r = raw("https://x.com/1", "Ether jumps")
    await bus.publish(
        T.RAW_NEWS, Envelope(kind="news", ts_ms=5, seq=1, data=r.model_dump()).model_dump()
    )
    await bus.publish(T.RAW_NEWS, Envelope(kind="watermark", ts_ms=9).model_dump())
    await bus.publish(T.RAW_NEWS, Envelope(kind="eos", ts_ms=9).model_dump())
    await task
    await bus.close()
    kinds = [m["kind"] async for m in out]
    assert kinds == ["news", "watermark", "eos"]

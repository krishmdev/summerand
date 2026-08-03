"""
rss_spider.py  –  Sprint‑1 (crypto‑wide edition)
================================================

Ingests *three* news sources and publishes every item to the `raw_news`
Kafka topic.

    • Crypto‑specialist RSS feeds      (CoinDesk, CoinTelegraph, …)
    • (Optional) CryptoPanic JSON API  (requires CRYPTOPANIC_TOKEN)

All items are normalised into a common dict:
    {id, ts, title, url, source}

The rest of the pipeline (ETL, embeddings, etc.) stays unchanged.
"""

from __future__ import annotations
import aiohttp
import asyncio
import datetime as dt
import feedparser
import hashlib
import json
import os
import time
from typing import Iterable

from aiokafka import AIOKafkaProducer

#test
print("rss_spider.py started")

# ───────────────────────────────────────────────────────────────────────────────
# 1) Broad editorial crypto‑feeds
RSS_BASE: list[str] = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.theblock.co/feeds/rss",
    "https://cryptonews.com/news/feed",
    "https://www.reuters.com/rssFeed/cryptocurrency",
]

# Final RSS list (editorial feeds only for now)
RSS_URLS: list[str] = RSS_BASE


# 3) CryptoPanic API (optional)
CRYPTOPANIC_TOKEN = os.getenv("CRYPTOPANIC_TOKEN")
CRYPTOPANIC_URL = (
    f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_TOKEN}&public=true"
    if CRYPTOPANIC_TOKEN
    else None
)

# Kafka
KAFKA_BROKER = os.getenv("KAFKA_BROKER", "redpanda:9092")
print(f"[DEBUG] KAFKA_BROKER value: {repr(KAFKA_BROKER)}")
TOPIC = "raw_news"
POLL_INTERVAL = 30  # seconds between RSS polls
HEADERS = {"User-Agent": "SummerandRSS/1.0"}

# ───────────────────────────────────────────────────────────────────────────────
async def fetch_rss(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, timeout=10, headers=HEADERS) as resp:
        resp.raise_for_status()
        return await resp.text()

async def consume_rss(producer: AIOKafkaProducer) -> None:
    while True:
        async with aiohttp.ClientSession() as session:
            for url in RSS_URLS:
                try:
                    xml = await fetch_rss(session, url)
                    feed = feedparser.parse(xml)
                except Exception as exc:
                    print(f"[rss] WARN {url} → {exc}")
                    continue

                for entry in feed.entries:
                    uid = hashlib.sha256(entry.title.encode()).hexdigest()
                    await producer.send_and_wait(
                        TOPIC,
                        {
                            "id": uid,
                            "ts": int(time.time() * 1000),
                            "title": entry.title,
                            "url": entry.link,
                            "source": url,
                        },
                    )
        await asyncio.sleep(POLL_INTERVAL)

# ───────────────────────────────────────────────────────────────────────────────
async def consume_cryptopanic(producer: AIOKafkaProducer) -> None:
    """Continuously poll CryptoPanic JSON API if token is set."""
    if not CRYPTOPANIC_URL:
        return  # token not provided; skip
    last_seen: set[str] = set()
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        while True:
            try:
                async with session.get(CRYPTOPANIC_URL, timeout=10) as resp:
                    data = await resp.json()
                for post in data.get("results", []):
                    if post["id"] in last_seen:
                        continue
                    last_seen.add(post["id"])
                    await producer.send_and_wait(
                        TOPIC,
                        {
                            "id": str(post["id"]),
                            "ts": int(
                                dt.datetime.fromisoformat(post["published_at"].rstrip("Z"))
                                .timestamp()
                                * 1000
                            ),
                            "title": post["title"],
                            "url": post["url"],
                            "source": "cryptopanic",
                        },
                    )
                # CryptoPanic free API refreshes roughly every minute
            except Exception as exc:
                print(f"[cryptopanic] WARN {exc}")
            await asyncio.sleep(60)

# ───────────────────────────────────────────────────────────────────────────────
async def main() -> None:
    print(f"[DEBUG] Creating producer with bootstrap_servers: {repr(KAFKA_BROKER)}")
    producer = AIOKafkaProducer(
        bootstrap_servers=KAFKA_BROKER,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
    )
    await producer.start()
    try:
        tasks = [asyncio.create_task(consume_rss(producer))]
        if CRYPTOPANIC_URL:
            tasks.append(asyncio.create_task(consume_cryptopanic(producer)))
        await asyncio.gather(*tasks)
    finally:
        await producer.stop()

if __name__ == "__main__":
    asyncio.run(main())
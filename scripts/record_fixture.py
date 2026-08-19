"""Record the demo fixture: one poll of every enabled RSS feed (keeping the last N hours by publish
time) plus Coinbase 1-minute candles for BTC/ETH/SOL, and Polygon minute aggregates for a few
equities when POLYGON_API_KEY works. Needs network; takes a couple of minutes.

    uv run python scripts/record_fixture.py --hours 24 --out fixtures/demo_feed.jsonl.gz

Only title, url, source, publish time and a 240-character plain-text summary are kept per item.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import time
from collections import Counter
from pathlib import Path

import httpx

from summerand.config import REPO_ROOT, load_sources
from summerand.etl.etl_service import strip_html, truncate
from summerand.ingest.rss_spider import USER_AGENT, parse_feed
from summerand.market.coinbase_ws import fetch_candles
from summerand.schemas import MINUTE_MS, ms_to_iso

POLYGON_SYMBOLS = ["SPY", "NVDA", "AAPL", "MSFT", "COIN", "TSLA", "MSTR"]


async def record_news(client: httpx.AsyncClient, start: int, end: int) -> tuple[list[dict], dict]:
    sources = load_sources(REPO_ROOT / "config" / "sources.yaml")
    rows, status = [], {}
    for feed in sources.enabled_feeds:
        try:
            resp = await client.get(feed.url, timeout=20)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            status[feed.name] = f"error: {type(exc).__name__}"
            continue
        items = parse_feed(resp.content, feed, observed_ms=end)
        kept = 0
        for it in items:
            if it.ts_inferred or not start <= it.published_ms < end:
                continue
            rows.append(
                {
                    "type": "news",
                    "ts_ms": it.published_ms,
                    "published_ms": it.published_ms,
                    "source": it.source,
                    "title": strip_html(it.title),
                    "url": it.url,
                    "summary": truncate(strip_html(it.summary), 240),
                }
            )
            kept += 1
        status[feed.name] = f"{kept}/{len(items)} in window"
    return rows, status


async def record_polygon(
    client: httpx.AsyncClient, key: str, start: int, end: int
) -> tuple[list[dict], dict]:
    rows, status = [], {}
    for i, sym in enumerate(POLYGON_SYMBOLS):
        if i:
            await asyncio.sleep(13)  # free plan: 5 requests per minute
        resp = await client.get(
            f"https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/minute/{start}/{end - 1}",
            params={"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": key},
            timeout=30,
        )
        if resp.status_code != 200:
            status[sym] = f"http {resp.status_code}"
            if resp.status_code in (401, 403):
                break
            continue
        results = resp.json().get("results") or []
        for r in results:
            rows.append(
                {
                    "type": "bar",
                    "ts_ms": r["t"] + MINUTE_MS,
                    "start_ms": r["t"],
                    "symbol": sym,
                    "o": r["o"],
                    "h": r["h"],
                    "l": r["l"],
                    "c": r["c"],
                    "v": r["v"],
                    "venue": "polygon",
                }
            )
        status[sym] = f"{len(results)} bars"
    return rows, status


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "fixtures" / "demo_feed.jsonl.gz")
    args = ap.parse_args()

    end = int(time.time() * 1000) // MINUTE_MS * MINUTE_MS
    start = end - int(args.hours * 3600 * 1000)
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        news, feed_status = await record_news(client, start, end)
        bars = []
        for product in ("BTC-USD", "ETH-USD", "SOL-USD"):
            for b in await fetch_candles(client, product, start, end):
                bars.append(
                    {
                        "type": "bar",
                        "ts_ms": b.end_ms,
                        "start_ms": b.ts_ms,
                        "symbol": b.symbol,
                        "o": b.open,
                        "h": b.high,
                        "l": b.low,
                        "c": b.close,
                        "v": b.volume,
                        "venue": "coinbase",
                    }
                )
        polygon_status = {}
        key = os.environ.get("POLYGON_API_KEY", "").strip()
        if key:
            eq, polygon_status = await record_polygon(client, key, start, end)
            bars.extend(eq)

    seen, deduped = set(), []
    for row in sorted(news, key=lambda r: (r["ts_ms"], r["url"])):
        if row["url"] not in seen:
            seen.add(row["url"])
            deduped.append(row)
    rows = deduped + [b for b in bars if start < b["ts_ms"] <= end]
    rows.sort(key=lambda r: (r["ts_ms"], r["type"], r.get("symbol", ""), r.get("url", "")))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(args.out, "wb", mtime=0) as fh:
        fh.write(
            "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows).encode()
        )

    manifest = {
        "recorded_at": ms_to_iso(end),
        "window": [ms_to_iso(start), ms_to_iso(end)],
        "news": len(deduped),
        "news_by_source": dict(sorted(Counter(r["source"] for r in deduped).items())),
        "bars_by_symbol": dict(
            sorted(Counter(r["symbol"] for r in rows if r["type"] == "bar").items())
        ),
        "feed_status": feed_status,
        "polygon_status": polygon_status or "POLYGON_API_KEY not set",
        "bytes": args.out.stat().st_size,
    }
    manifest_path = args.out.with_name(args.out.name.replace(".jsonl.gz", ".manifest.json"))
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

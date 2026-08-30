"""Optional CryptoPanic poller. Needs CRYPTOPANIC_TOKEN; the endpoint is configurable."""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections import OrderedDict
from typing import Any

import httpx

from summerand.bus import Bus
from summerand.bus import topics as T
from summerand.clock import Clock, WallClock
from summerand.etl.dedup import article_id
from summerand.ingest.errors import PermanentFeedError
from summerand.schemas import Envelope, RawNews, iso_to_ms

log = logging.getLogger(__name__)


def parse_posts(data: dict[str, Any], observed_ms: int) -> list[RawNews]:
    items = []
    for post in data.get("results", []):
        url = post.get("original_url") or post.get("url")
        title = post.get("title")
        if not url or not title:
            continue
        published = post.get("published_at") or post.get("created_at")
        domain = (post.get("source") or {}).get("domain")
        items.append(
            RawNews(
                id=article_id(url),
                source=f"cryptopanic:{domain}" if domain else "cryptopanic",
                title=title,
                url=url,
                summary=post.get("description") or "",
                # Parsed as UTC; a naive fromisoformat would be read as local time.
                published_ms=iso_to_ms(published) if published else observed_ms,
                observed_ms=observed_ms,
                ts_inferred=published is None,
            )
        )
    return items


async def run_cryptopanic(
    bus: Bus,
    client: httpx.AsyncClient,
    url_template: str,
    token: str,
    poll_seconds: int = 120,
    clock: Clock | None = None,
) -> None:
    clock = clock or WallClock()
    url = url_template.format(token=token)
    seen: OrderedDict[str, None] = OrderedDict()
    seq = itertools.count()
    while True:
        # Never log the URL or the exception text: both carry the token.
        try:
            resp = await client.get(url, timeout=15)
        except httpx.HTTPError as exc:
            log.warning("cryptopanic poll failed: %s", type(exc).__name__)
            resp = None
        items = []
        if resp is not None:
            if resp.status_code in (401, 403, 404):
                raise PermanentFeedError(
                    f"cryptopanic returned HTTP {resp.status_code}; "
                    "check CRYPTOPANIC_URL and the token's plan"
                )
            if resp.status_code != 200:
                log.warning("cryptopanic poll failed: HTTP %d", resp.status_code)
            else:
                try:
                    items = parse_posts(resp.json(), clock.now_ms())
                except ValueError:
                    log.warning("cryptopanic returned a body that isn't JSON")
        for item in items:
            if item.id in seen:
                continue
            seen[item.id] = None
            if len(seen) > 5000:
                seen.popitem(last=False)
            env = Envelope(
                kind="news", ts_ms=item.observed_ms, seq=next(seq), data=item.model_dump()
            )
            await bus.publish(T.RAW_NEWS, env.model_dump(), key=item.id)
        await asyncio.sleep(poll_seconds)

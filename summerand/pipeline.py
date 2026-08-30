"""clean_news + bars/ticks -> window -> embeddings -> clusters -> ranked stories -> briefs.

Every periodic job runs on the event-time scheduler:
    recluster  every 60s   (k re-selected by silhouette every 10 min)
    rank       every 30s
    brief      every 5 min (cluster briefs refresh on >30% membership change or after 30 min)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from summerand.brief.generate import Briefer, membership_hash
from summerand.bus import Bus
from summerand.bus import topics as T
from summerand.clock import HOUR_MS, MINUTE_MS, SECOND_MS, EventClock, Scheduler
from summerand.config import SourcesConfig
from summerand.driver import EventTimeDriver
from summerand.market.bars import BarBuilder, PriceBook
from summerand.nlp.cluster import WindowClusterer
from summerand.nlp.embed import embed_text
from summerand.nlp.index import EmbeddingIndex
from summerand.nlp.label import ctfidf_terms, headline_index
from summerand.rank.loop import Ranker, hhmm
from summerand.schemas import Bar, CleanNews, Envelope, Tick
from summerand.store.repo import Store

log = logging.getLogger(__name__)

RECLUSTER_MS = 60 * SECOND_MS
RANK_MS = 30 * SECOND_MS
BRIEF_MS = 5 * MINUTE_MS
K_SELECT_MS = 10 * MINUTE_MS
BRIEF_MAX_AGE_MS = 30 * MINUTE_MS
BRIEF_CHANGE = 0.3
BRIEF_TOP = 8
CATCH_UP_MS = 5 * MINUTE_MS
MARKET_SYMBOLS = ["BTC", "ETH", "SOL", "SPY", "NVDA", "AAPL", "COIN"]


@dataclass
class PipelineConfig:
    window_ms: int = 6 * HOUR_MS
    memory_ms: int = 26 * HOUR_MS
    top_n: int = 20


class Pipeline:
    def __init__(
        self,
        *,
        store: Store,
        bus: Bus,
        index: EmbeddingIndex,
        sources: SourcesConfig,
        briefer: Briefer | None = None,
        clusterer: WindowClusterer | None = None,
        config: PipelineConfig | None = None,
        live: bool = False,
        wall_ms: Any = None,
    ) -> None:
        self.store = store
        self.live = live
        self._wall_ms = wall_ms or (lambda: time.time_ns() // 1_000_000)
        self.bus = bus
        self.index = index
        self.cfg = config or PipelineConfig()
        self.clusterer = clusterer or WindowClusterer()
        self.briefer = briefer or Briefer()
        self.ranker = Ranker(sources, top_n=self.cfg.top_n)
        self.clock = EventClock()
        self.scheduler = Scheduler(self.clock)
        self.scheduler.every("recluster", RECLUSTER_MS, self.recluster, priority=0)
        self.scheduler.every("rank", RANK_MS, self.rank, priority=1)
        self.scheduler.every("brief", BRIEF_MS, self.brief, priority=2)
        self.articles: dict[str, CleanNews] = {}
        self.prices = PriceBook(max_age_ms=30 * MINUTE_MS)
        self.bar_builder = BarBuilder()
        self._new_articles: list[CleanNews] = []
        self._new_bars: list[Bar] = []
        self.labels: dict[str, str] = {}
        self.headlines: dict[str, str] = {}
        self.last_select_ms: int | None = None
        self.last_good: dict[str, Any] | None = None
        self.cluster_briefs: dict[str, dict[str, Any]] = {}
        self._brief_members: dict[str, tuple[frozenset[str], int]] = {}
        self.market: dict[str, Any] | None = None
        self.snapshot_log: list[list[Any]] = []
        self.brief_log: list[list[Any]] = []
        self.stats = {"news": 0, "bars": 0, "ticks": 0, "stale_snapshots": 0, "regenerations": 0}
        self.job_seconds: dict[str, float] = {}
        self.late_dropped: Counter[str] = Counter()

    # inputs ------------------------------------------------------------------------------------

    async def handle(self, topic: str, env: Envelope) -> None:
        if env.kind == "news":
            item = CleanNews.model_validate(env.data)
            if item.id not in self.articles:
                self.articles[item.id] = item
                self._new_articles.append(item)
                self.stats["news"] += 1
        elif env.kind == "bar":
            bar = Bar.model_validate(env.data)
            self.prices.add(bar)
            self._new_bars.append(bar)
            self.stats["bars"] += 1
        elif env.kind == "tick":
            self.stats["ticks"] += 1
            for bar in self.bar_builder.add(Tick.model_validate(env.data)):
                self.prices.add(bar)
                self._new_bars.append(bar)

    def _flush_writes(self, now_ms: int) -> None:
        for bar in self.bar_builder.flush(now_ms):
            self.prices.add(bar)
            self._new_bars.append(bar)
        if self._new_articles:
            self.store.save_articles(self._new_articles)
            self._new_articles = []
        if self._new_bars:
            self.store.save_bars(self._new_bars)
            self._new_bars = []

    def catching_up(self, now_ms: int) -> bool:
        """Live mode after a restart: we're re-reading retained history. Don't pay for LLM briefs
        or push stale rankings to clients until event time is within 5 minutes of wall time."""
        return self.live and now_ms < self._wall_ms() - CATCH_UP_MS

    async def _publish(self, topic: str, env: Envelope) -> None:
        if not self.catching_up(env.ts_ms):
            await self.bus.publish(topic, env.model_dump())

    def finish(self) -> None:
        """Persist whatever arrived after the last recluster tick (end of a replay)."""
        if self.clock.started:
            self._flush_writes(self.clock.now_ms() + MINUTE_MS)

    def window_ids(self, now_ms: int) -> list[str]:
        lo = now_ms - self.cfg.window_ms
        inside = [a for a in self.articles.values() if lo <= a.published_ms <= now_ms]
        return [a.id for a in sorted(inside, key=lambda a: (a.published_ms, a.id))]

    # jobs --------------------------------------------------------------------------------------

    async def recluster(self, now_ms: int) -> None:
        t0 = time.perf_counter()
        self._flush_writes(now_ms)
        ids = self.window_ids(now_ms)
        for i in ids:
            self.index.add(i, embed_text(self.articles[i]))
        self.index.evict(set(ids))
        for old in [
            i for i, a in self.articles.items() if a.published_ms < now_ms - self.cfg.memory_ms
        ]:
            del self.articles[old]
        self.prices.prune(now_ms - self.cfg.memory_ms)
        status = await self.index.sync()
        if status == "regenerated":
            self.stats["regenerations"] += 1
        if self.index.degraded:
            return
        select = (
            status == "regenerated"
            or self.last_select_ms is None
            or now_ms - self.last_select_ms >= K_SELECT_MS
        )
        present, X = self.index.matrix(ids)
        state = self.clusterer.refit(
            now_ms,
            present,
            X,
            generation=self.index.generation,
            embedder_id=self.index.gen.embedder_id,
            outlier_cos=self.index.embedder.outlier_cos,
            select=select,
        )
        if select:
            self.last_select_ms = now_ms
        docs = {
            cid: [self.articles[m].title for m in c.members] for cid, c in state.clusters.items()
        }
        terms = ctfidf_terms(docs)
        self.labels = {cid: ", ".join(t) for cid, t in terms.items()}
        self.headlines = {}
        for cid, c in state.clusters.items():
            _, Xc = self.index.matrix(c.members)
            self.headlines[cid] = c.members[headline_index(Xc, c.centroid)]
        self.store.save_clusters(
            [
                {
                    "id": cid,
                    "generation": c.generation,
                    "embedder_id": c.embedder_id,
                    "label": self.labels.get(cid, ""),
                    "headline_id": self.headlines.get(cid),
                    "size": len(c.members),
                    "born_ms": c.born_ms,
                    "updated_ms": now_ms,
                    "retired_ms": None,
                    "members": c.members,
                }
                for cid, c in state.clusters.items()
            ],
            {cid: now_ms for cid in state.retired},
        )
        self.job_seconds["recluster"] = (
            self.job_seconds.get("recluster", 0) + time.perf_counter() - t0
        )

    def _meta(self, now_ms: int, stale: bool) -> dict[str, Any]:
        return {
            "type": "snapshot",
            "ts_ms": now_ms,
            "stale": stale,
            "generation": self.index.generation,
            "embedder_id": self.index.gen.embedder_id,
            "window_size": len(self.index.gen.vectors),
        }

    async def rank(self, now_ms: int) -> None:
        state = self.clusterer.state
        if self.index.degraded and self.last_good is not None:
            # Embeddings are down: serve the last good ranking as-is, flagged stale.
            snap = {**self.last_good, "ts_ms": now_ms, "stale": True}
            self.stats["stale_snapshots"] += 1
        elif state is None:
            return
        else:
            stories = self.ranker.rank(
                now_ms, state, self.articles, self.prices, self.labels, self.headlines
            )
            for s in stories:
                s["brief"] = self.cluster_briefs.get(s["cluster_id"])
            snap = {**self._meta(now_ms, False), "as_of_ms": now_ms, "stories": stories}
            self.last_good = snap
        self.store.save_snapshot(snap)
        self.snapshot_log.append(
            [
                now_ms,
                snap["stale"],
                [
                    [s["cluster_id"], s["rank"], s["score"], sorted(s["components"].items())]
                    for s in snap["stories"]
                ],
            ]
        )
        await self._publish(T.RANKED_STORIES, Envelope(kind="snapshot", ts_ms=now_ms, data=snap))

    def _items(self, member_ids: list[str], limit: int = 8) -> list[dict[str, Any]]:
        members = [self.articles[i] for i in member_ids if i in self.articles]
        members = sorted(members, key=lambda a: (-a.published_ms, a.id))[:limit]
        members.sort(key=lambda a: (a.published_ms, a.id))
        return [
            {
                "n": n,
                "id": a.id,
                "source": a.source,
                "time": f"{hhmm(a.published_ms)} UTC",
                "title": a.title,
                "summary": a.summary,
                "url": a.url,
            }
            for n, a in enumerate(members, 1)
        ]

    async def brief(self, now_ms: int) -> None:
        snap = self.last_good
        state = self.clusterer.state
        if snap is None or state is None or snap["stale"]:
            return
        for story in snap["stories"][:BRIEF_TOP]:
            cid = story["cluster_id"]
            cluster = state.clusters.get(cid)
            if cluster is None:
                continue
            members = frozenset(cluster.members)
            prev = self._brief_members.get(cid)
            if prev is not None:
                old, at = prev
                change = 1 - len(old & members) / len(old | members)
                if change <= BRIEF_CHANGE and now_ms - at < BRIEF_MAX_AGE_MS:
                    continue
            items = self._items(cluster.members)
            facts = [m["text"] for m in story["moves"]]
            facts.append(
                f"{story['size']} reports from {len(story['sources'])} sources since "
                f"{hhmm(story['first_ms'])} UTC"
            )
            result = await self.briefer.cluster_brief(
                cid, cluster.members, items, facts, use_llm=not self.catching_up(now_ms)
            )
            brief = {
                "kind": "cluster",
                "cluster_id": cid,
                "ts_ms": now_ms,
                "text": result.text,
                "method": result.method,
                "problems": result.problems,
                "facts": facts,
                "membership_hash": membership_hash(cluster.members),
                "sources": [
                    {k: it[k] for k in ("n", "id", "title", "url", "source")} for it in items
                ],
            }
            self._brief_members[cid] = (members, now_ms)
            self.cluster_briefs[cid] = brief
            story["brief"] = brief
            self.store.save_brief(brief)
            self._log_brief(brief)
            await self._publish(
                T.BRIEFS, Envelope(kind="cluster_update", ts_ms=now_ms, data={"story": story})
            )
        for cid in [c for c in self.cluster_briefs if c not in state.clusters]:
            self.cluster_briefs.pop(cid, None)
            self._brief_members.pop(cid, None)
        await self._market_brief(now_ms, snap)

    def market_facts(self, now_ms: int) -> list[str]:
        facts = []
        for sym in MARKET_SYMBOLS:
            r1 = self.prices.log_return(sym, now_ms - HOUR_MS, now_ms)
            r6 = self.prices.log_return(sym, now_ms - 6 * HOUR_MS, now_ms)
            if r1 is None:
                continue
            text = f"{sym} {math.expm1(r1) * 100:+.1f}% over 1h"
            if r6 is not None:
                text += f" and {math.expm1(r6) * 100:+.1f}% over 6h"
            facts.append(text)
        return facts

    async def _market_brief(self, now_ms: int, snap: dict[str, Any]) -> None:
        top = snap["stories"][:5]
        if not top:
            return
        items = [
            {
                "n": s["rank"],
                "id": s["headline"]["id"],
                "source": s["headline"]["source"],
                "time": f"{hhmm(s['headline']['published_ms'])} UTC",
                "title": s["headline"]["title"],
                "summary": "",
                "url": s["headline"]["url"],
                "cluster_id": s["cluster_id"],
            }
            for s in top
        ]
        facts = self.market_facts(now_ms)
        result = await self.briefer.market_brief(items, facts, use_llm=not self.catching_up(now_ms))
        brief = {
            "kind": "market",
            "cluster_id": None,
            "ts_ms": now_ms,
            "text": result.text,
            "method": result.method,
            "problems": result.problems,
            "facts": facts,
            "sources": [
                {k: it[k] for k in ("n", "id", "title", "url", "source", "cluster_id")}
                for it in items
            ],
        }
        self.market = brief
        self.store.save_brief(brief)
        self._log_brief(brief)
        await self._publish(T.BRIEFS, Envelope(kind="market_brief", ts_ms=now_ms, data=brief))

    def _log_brief(self, brief: dict[str, Any]) -> None:
        self.brief_log.append(
            [
                brief["ts_ms"],
                brief["kind"],
                brief["cluster_id"],
                hashlib.sha256(brief["text"].encode()).hexdigest(),
            ]
        )

    # replay equivalence ------------------------------------------------------------------------

    def digest(self) -> dict[str, Any]:
        state = self.clusterer.state
        clusters = {cid: c.members for cid, c in sorted(state.clusters.items())} if state else {}
        return {
            "clusters": clusters,
            "retired": sorted(self.clusterer.retired),
            "snapshots": self.snapshot_log,
            "briefs": self.brief_log,
            "late_dropped": dict(sorted(self.late_dropped.items())),
        }

    def digest_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.digest(), sort_keys=True).encode()).hexdigest()

    def status(self, driver: EventTimeDriver | None = None) -> dict[str, Any]:
        return {
            "event_ms": self.clock.now_ms() if self.clock.started else None,
            "generation": self.index.generation,
            "embedder_id": self.index.gen.embedder_id,
            "degraded": self.index.degraded,
            "window_size": len(self.index.gen.vectors),
            "clusters": len(self.clusterer.state.clusters) if self.clusterer.state else 0,
            "k": self.clusterer.k,
            "late_dropped": dict(driver.late_dropped) if driver else {},
            "llm": self.briefer.state,
            "jobs_fired": dict(self.scheduler.fired),
            **self.stats,
        }


async def run_pipeline(
    bus: Bus,
    pipeline: Pipeline,
    inputs: list[str],
    *,
    lateness_ms: int = 2 * MINUTE_MS,
    live: bool = False,
    idle_after_s: float = 30.0,
    absent_warn_s: float = 60.0,
    poll_s: float = 5.0,
    group: str | None = None,
    on_driver: Any = None,
    subs: dict[str, Any] | None = None,
    wall_ms: Any = None,
    wall_advance: set[str] | None = None,
) -> EventTimeDriver:
    """Feed the pipeline from bus topics until every input has sent end-of-stream.

    Live mode runs forever and ignores end-of-stream markers (a retained topic may still hold one
    from an old replay). An input counts as idle only when nothing from it is waiting in our
    queue, its subscription has caught up with the broker, and nothing has arrived for
    idle_after_s; then its watermark moves to wall time minus the lateness, so a quiet feed
    doesn't stall event time. A backlog is never mistaken for silence.

    clean_news is excluded by default: its backlog can sit upstream in the ETL where we can't
    see it, so it only advances from the watermarks the RSS ingester emits."""
    if wall_advance is None:
        wall_advance = {t for t in inputs if t != T.CLEAN_NEWS}
    wall_ms = wall_ms or (lambda: time.time_ns() // 1_000_000)
    driver = EventTimeDriver(
        pipeline.scheduler, pipeline.handle, inputs, lateness_ms, ignore_eos=live
    )
    pipeline.late_dropped = driver.late_dropped
    if on_driver:
        on_driver(driver)
    queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
    started = time.monotonic()
    arrived: dict[str, float | None] = dict.fromkeys(inputs)
    pending = dict.fromkeys(inputs, 0)
    warned: set[str] = set()
    subs = subs or {t: bus.subscribe(t, group) for t in inputs}

    async def pump(topic: str) -> None:
        async for msg in subs[topic]:
            arrived[topic] = time.monotonic()
            pending[topic] += 1
            await queue.put((topic, msg))
            if msg.get("kind") == "eos" and not live:
                return

    async def caught_up(topic: str) -> bool:
        check = getattr(subs[topic], "caught_up", None)
        return True if check is None else await check()

    pumps = [asyncio.create_task(pump(t)) for t in inputs]
    try:
        while not driver.finished:
            try:
                topic, msg = await asyncio.wait_for(
                    queue.get(), timeout=poll_s if live else absent_warn_s
                )
            except TimeoutError:
                topic = None
            if topic is not None:
                pending[topic] -= 1
                await driver.offer(topic, Envelope.model_validate(msg))
            now = time.monotonic()
            for t in inputs:
                if arrived[t] is None and now - started > absent_warn_s and t not in warned:
                    warned.add(t)
                    log.warning(
                        "input %s has produced nothing after %.0fs; event time can't advance "
                        "past it%s",
                        t,
                        absent_warn_s,
                        "" if live else " (replay mode)",
                    )
            if live:
                for t in (t for t in inputs if t in wall_advance):
                    quiet = now - (arrived[t] or started) > idle_after_s
                    # Safe to move the watermark: nothing from t is waiting in our queue or on
                    # the broker, so no un-offered event can fall behind it. Events already
                    # offered sit in the driver's heap and are released in (ts, seq) order, so
                    # they need no clamp.
                    if quiet and pending[t] == 0 and await caught_up(t):
                        await driver.advance_idle(t, wall_ms() - lateness_ms)
    finally:
        for p in pumps:
            p.cancel()
        for s in subs.values():
            await s.close()
    if driver.finished:
        pipeline.finish()
    return driver

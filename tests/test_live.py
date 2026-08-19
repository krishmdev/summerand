"""Live-mode behaviour of run_pipeline and the driver (review items: idle vs backlog, stale
end-of-stream markers, absent inputs, arrival interleavings)."""

import asyncio
import logging
import random
import time

from summerand.api.ws import Hub
from summerand.bus import InMemoryBus
from summerand.clock import MINUTE_MS, EventClock, Scheduler
from summerand.driver import EventTimeDriver
from summerand.pipeline import run_pipeline
from summerand.schemas import Envelope


class FakePipeline:
    def __init__(self, slow_s=0.0):
        self.clock = EventClock()
        self.scheduler = Scheduler(self.clock)
        self.seen = []
        self.slow_s = slow_s

    async def handle(self, topic, env):
        self.seen.append((topic, env.ts_ms))
        if self.slow_s:
            await asyncio.sleep(self.slow_s)

    def finish(self):
        pass


def news(ts, seq):
    return Envelope(kind="news", ts_ms=ts, seq=seq).model_dump()


async def run_live_until(bus, pipe, inputs, done, **kw):
    box = {}
    task = asyncio.create_task(
        run_pipeline(bus, pipe, inputs, live=True, on_driver=lambda d: box.setdefault("d", d), **kw)
    )
    deadline = time.monotonic() + 20
    while not done() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    task.cancel()
    return box["d"]


async def test_backlog_is_not_mistaken_for_an_idle_topic():
    """News sits in our queue behind slow tick processing for longer than idle_after_s. The old
    code marked news idle and dropped the whole backlog as late."""
    bus = InMemoryBus()
    pipe = FakePipeline(slow_s=0.05)
    wall = int(time.time() * 1000)
    old = wall - 3600_000
    subs = {t: bus.subscribe(t) for t in ("ticks_crypto", "clean_news")}
    for i in range(10):
        await bus.publish("ticks_crypto", Envelope(kind="tick", ts_ms=old + i, seq=i).model_dump())
    for i in range(5):
        await bus.publish("clean_news", news(old + 1000 + i, 100 + i))
    driver = await run_live_until(
        bus,
        pipe,
        ["ticks_crypto", "clean_news"],
        done=lambda: sum(1 for t, _ in pipe.seen if t == "clean_news") == 5,
        subs=subs,
        idle_after_s=0.01,
        poll_s=0.05,
    )
    assert dict(driver.late_dropped) == {}
    assert sum(1 for t, _ in pipe.seen if t == "clean_news") == 5


async def test_live_mode_ignores_a_stale_end_of_stream():
    bus = InMemoryBus()
    pipe = FakePipeline()
    subs = {"clean_news": bus.subscribe("clean_news")}
    wall = int(time.time() * 1000)
    await bus.publish("clean_news", news(wall - 600_000, 1))
    await bus.publish("clean_news", Envelope(kind="eos", ts_ms=wall).model_dump())
    await bus.publish("clean_news", news(wall - 500_000, 2))
    driver = await run_live_until(
        bus,
        pipe,
        ["clean_news"],
        done=lambda: len(pipe.seen) == 2,
        subs=subs,
        idle_after_s=0.01,
        poll_s=0.05,
    )
    assert len(pipe.seen) == 2 and not driver.finished


async def test_absent_input_is_reported(caplog):
    bus = InMemoryBus()
    pipe = FakePipeline()
    subs = {t: bus.subscribe(t) for t in ("clean_news", "ticks_crypto")}
    await bus.publish("clean_news", news(1000, 1))
    caplog.set_level(logging.WARNING, logger="summerand.pipeline")
    task = asyncio.create_task(
        run_pipeline(bus, pipe, ["clean_news", "ticks_crypto"], subs=subs, absent_warn_s=0.1)
    )
    await asyncio.sleep(0.4)
    task.cancel()
    assert any("ticks_crypto has produced nothing" in r.message for r in caplog.records)
    assert pipe.seen == []  # replay mode: nothing is released while an input is silent


async def test_idle_advance_releases_already_buffered_events_in_order():
    pipe = FakePipeline()
    d = EventTimeDriver(pipe.scheduler, pipe.handle, ["a", "b"], lateness_ms=MINUTE_MS)
    await d.offer("a", Envelope(kind="news", ts_ms=10 * MINUTE_MS + 5, seq=2))
    await d.offer("a", Envelope(kind="news", ts_ms=10 * MINUTE_MS, seq=1))
    await d.advance_idle("b", 100 * MINUTE_MS)
    await d.advance_idle("a", 100 * MINUTE_MS)
    assert [ts for _, ts in pipe.seen] == [10 * MINUTE_MS, 10 * MINUTE_MS + 5]
    assert d.late_dropped == {}


async def test_release_is_identical_across_shuffled_arrivals():
    """Per-topic streams are shuffled within the lateness bound, topics are interleaved at random,
    and one record is later than the bound. Every run must release the same sequence."""
    lateness = 2 * MINUTE_MS
    rng0 = random.Random(0)
    streams = {
        t: [(i * 20_000 + rng0.randrange(0, 5_000), i) for i in range(60)]
        for t in ("clean_news", "bars", "ticks_crypto")
    }

    def arrivals(seed):
        rng = random.Random(seed)
        per_topic = {}
        for t, evs in streams.items():
            evs = sorted(evs)
            # local shuffle: swap neighbours closer together than the lateness bound
            for j in range(len(evs) - 1):
                if rng.random() < 0.5 and evs[j + 1][0] - evs[j][0] < lateness // 2:
                    evs[j], evs[j + 1] = evs[j + 1], evs[j]
            per_topic[t] = [Envelope(kind="news", ts_ms=ts, seq=s) for ts, s in evs]
        per_topic["clean_news"].append(Envelope(kind="news", ts_ms=5 * 20_000, seq=999))  # late
        order = [t for t, evs in per_topic.items() for _ in evs]
        rng.shuffle(order)
        return per_topic, order

    async def run(seed):
        pipe = FakePipeline()
        fired = []
        pipe.scheduler.every("rank", 30_000, lambda t: fired.append(t))
        d = EventTimeDriver(pipe.scheduler, pipe.handle, list(streams), lateness_ms=lateness)
        per_topic, order = arrivals(seed)
        idx = dict.fromkeys(per_topic, 0)
        for t in order:
            await d.offer(t, per_topic[t][idx[t]])
            idx[t] += 1
        for t in per_topic:
            await d.offer(t, Envelope(kind="eos", ts_ms=0))
        return pipe.seen, fired, dict(d.late_dropped)

    results = [await run(seed) for seed in range(5)]
    assert all(r == results[0] for r in results)
    seen, fired, late = results[0]
    assert late == {"clean_news": 1}
    assert [ts for _, ts in seen] == sorted(ts for _, ts in seen)
    assert fired and fired == sorted(fired)


def test_cluster_ids_continue_after_restart(tmp_path):
    from summerand.store.repo import Store

    store = Store(f"sqlite:///{tmp_path / 's.sqlite'}")
    assert store.next_cluster_number() == 1
    row = {
        "generation": 1,
        "embedder_id": "x",
        "label": "",
        "headline_id": None,
        "size": 1,
        "born_ms": 0,
        "updated_ms": 0,
        "retired_ms": None,
        "members": [],
    }
    store.save_clusters([{**row, "id": "c00041"}, {**row, "id": "c00007"}], {})
    assert store.next_cluster_number() == 42


async def test_heartbeat_reports_as_of_time_without_a_clock():
    hub = Hub()
    hub.latest["snapshot"] = {"type": "snapshot", "ts_ms": 123, "stories": []}
    from summerand.api.ws import Client

    c = Client(ws=None)
    hub.clients.add(c)
    task = asyncio.create_task(hub.heartbeat(interval=0.01))
    msg = await asyncio.wait_for(c.queue.get(), 1)
    task.cancel()
    assert msg["type"] == "heartbeat" and msg["ts_ms"] == 123 and msg["wall_ms"] > 0

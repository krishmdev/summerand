from summerand.clock import MINUTE_MS, EventClock, Scheduler
from summerand.driver import EventTimeDriver
from summerand.schemas import Envelope


def ev(ts, seq=0, kind="news"):
    return Envelope(kind=kind, ts_ms=ts, seq=seq)


async def test_scheduler_fires_aligned_jobs_in_order():
    clock = EventClock()
    sched = Scheduler(clock)
    fired = []
    sched.every("rank", 30_000, lambda t: fired.append(("rank", t)), priority=1)
    sched.every("recluster", 60_000, lambda t: fired.append(("recluster", t)), priority=0)
    sched.start(10_000)
    await sched.advance_to(120_000)
    assert fired == [
        ("rank", 30_000),
        ("recluster", 60_000),
        ("rank", 60_000),
        ("rank", 90_000),
        ("recluster", 120_000),
        ("rank", 120_000),
    ]
    assert clock.now_ms() == 120_000


async def test_clock_never_moves_backwards():
    clock = EventClock(5)
    clock.set(10)
    try:
        clock.set(9)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


async def test_driver_reorders_within_lateness_and_drops_late():
    clock = EventClock()
    sched = Scheduler(clock)
    seen = []

    async def handle(topic, env):
        seen.append((env.ts_ms, clock.now_ms()))

    d = EventTimeDriver(sched, handle, ["a"], lateness_ms=2 * MINUTE_MS)
    base = 1_000_000
    await d.offer("a", ev(base + 60_000, 1))
    await d.offer("a", ev(base, 2))  # 60s out of order: fine
    await d.offer("a", ev(base + 10 * MINUTE_MS, 3))
    await d.offer("a", ev(base + 5 * MINUTE_MS, 4))  # 5 min behind the max: late
    await d.offer("a", Envelope(kind="eos", ts_ms=0))
    assert [ts for ts, _ in seen] == [base, base + 60_000, base + 10 * MINUTE_MS]
    assert all(ts == now for ts, now in seen)
    assert d.late_dropped["a"] == 1


async def test_driver_release_order_is_independent_of_topic_interleaving():
    events = {
        "news": [ev(t, s) for s, t in enumerate([1000, 70_000, 200_000, 400_000])],
        "bars": [
            ev(t, 100 + s, "bar")
            for s, t in enumerate([60_000, 120_000, 180_000, 240_000, 300_000])
        ],
    }

    async def run(order):
        clock = EventClock()
        sched = Scheduler(clock)
        out = []
        sched.every("job", 50_000, lambda t: out.append(("job", t)))

        async def handle(topic, env):
            out.append((topic, env.ts_ms))

        d = EventTimeDriver(sched, handle, ["news", "bars"], lateness_ms=10_000)
        idx = {"news": 0, "bars": 0}
        for topic in order:
            if idx[topic] < len(events[topic]):
                await d.offer(topic, events[topic][idx[topic]])
                idx[topic] += 1
        for topic in ("news", "bars"):
            while idx[topic] < len(events[topic]):
                await d.offer(topic, events[topic][idx[topic]])
                idx[topic] += 1
            await d.offer(topic, Envelope(kind="eos", ts_ms=0))
        return out, dict(d.late_dropped)

    a = await run(["news"] * 4 + ["bars"] * 5)
    b = await run(["bars"] * 5 + ["news"] * 4)
    c = await run(["news", "bars"] * 5)
    assert a == b == c
    assert a[1] == {}
    times = [t for _, t in a[0]]
    assert times == sorted(times)


async def test_scheduler_waits_for_first_event():
    clock = EventClock()
    sched = Scheduler(clock)
    fired = []
    seen = []
    sched.every("job", 10_000, fired.append)

    async def handle(topic, env):
        seen.append(clock.now_ms())

    d = EventTimeDriver(sched, handle, ["a", "b"], lateness_ms=0)
    await d.offer("a", Envelope(kind="watermark", ts_ms=50_000))
    assert not sched.started and fired == []
    await d.offer("b", ev(10_500))
    await d.offer("b", Envelope(kind="eos", ts_ms=0))
    await d.offer("a", Envelope(kind="eos", ts_ms=0))
    # The clock starts at the first event, so no job is due before it; the watermark from
    # topic a then carries time forward to 50s.
    assert seen == [10_500]
    assert fired == [20_000, 30_000, 40_000, 50_000]

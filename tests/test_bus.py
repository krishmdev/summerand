import asyncio

from summerand.bus import InMemoryBus


async def test_fan_out_and_close():
    bus = InMemoryBus()
    a = bus.subscribe("t", "g1")
    b = bus.subscribe("t", "g2")
    await bus.publish("t", {"n": 1})
    await bus.publish("t", {"n": 2})
    await bus.close()

    async def drain(sub):
        return [m["n"] async for m in sub]

    assert await asyncio.gather(drain(a), drain(b)) == [[1, 2], [1, 2]]


async def test_messages_are_copied_through_json():
    bus = InMemoryBus()
    sub = bus.subscribe("t")
    payload = {"xs": [1, 2]}
    await bus.publish("t", payload)
    payload["xs"].append(3)
    await bus.close()
    got = [m async for m in sub]
    assert got == [{"xs": [1, 2]}]

import os, asyncio, json, datetime, websockets
from aiokafka import AIOKafkaProducer

KEY    = os.getenv("POLYGON_API_KEY")
BROKER = os.getenv("KAFKA_BROKER", "redpanda:9092")
SYMS   = ["AAPL", "MSFT", "NVDA", "META", "COIN"]

URI = "wss://socket.polygon.io/stocks"

async def main():
    prod = AIOKafkaProducer(bootstrap_servers=BROKER,
                            value_serializer=lambda v: json.dumps(v).encode())
    await prod.start()
    try:
        async with websockets.connect(URI, ping_interval=15) as ws:
            await ws.send(json.dumps({"action": "auth", "params": KEY}))
            await ws.send(json.dumps({"action": "subscribe",
                                      "params": ",".join(f"T.{s}" for s in SYMS)}))
            async for raw in ws:
                for trade in json.loads(raw):
                    if trade["ev"] != "T":
                        continue
                    await prod.send_and_wait("ticks_equity", {
                        "symbol": trade["sym"],
                        "price":  trade["p"],
                        "size":   trade["s"],
                        "ts":     datetime.datetime.utcfromtimestamp(
                                    trade["t"]/1_000_000_000).isoformat()+"Z"
                    })
    finally:
        await prod.stop()

if __name__ == "__main__":
    asyncio.run(main())

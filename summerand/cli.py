"""Command line entry points.

summerand demo --speed 300          replay the recorded fixture, serve API + WebSocket
summerand live                      RSS + Coinbase in one process, no Kafka
summerand ingest rss|coinbase|polygon|cryptopanic   (compose: publish to Redpanda)
summerand etl | pipeline | api | replay             (compose services)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Annotated

import typer

from summerand.bus import topics as T
from summerand.config import REPO_ROOT, Settings
from summerand.egress import enforce

app = typer.Typer(add_completion=False, no_args_is_help=True)
ingest_app = typer.Typer(no_args_is_help=True, help="Run one ingest source against Kafka.")
app.add_typer(ingest_app, name="ingest")

DEFAULT_FIXTURE = REPO_ROOT / "fixtures" / "demo_feed.jsonl.gz"
log = logging.getLogger("summerand")


def _run_feed(name: str, coro_fn) -> None:
    """Run an optional ingest service. A permanent configuration problem ends it with exit 0 so
    compose doesn't restart it forever."""
    from summerand.ingest.errors import PermanentFeedError

    try:
        asyncio.run(coro_fn())
    except PermanentFeedError as exc:
        log.error("%s disabled: %s", name, exc)
        raise typer.Exit(0) from None


def _setup(process: str, verbose: bool = False, **overrides: object) -> Settings:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("aiokafka", "httpx", "uvicorn.access", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = Settings(**{k: v for k, v in overrides.items() if v is not None})
    enforce(settings.summerand_egress_check, process)
    return settings


async def _serve(fastapi_app: object, host: str, port: int) -> tuple[object, asyncio.Task]:
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(fastapi_app, host=host, port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.05)
    return server, task


async def smoke_check(base: str) -> dict[str, object]:
    """Hit the running server the way the extension does. Raises on any failure."""
    import httpx
    import websockets

    async with httpx.AsyncClient(base_url=base, timeout=10) as client:
        health = (await client.get("/healthz")).raise_for_status().json()
        stories = (await client.get("/api/stories", params={"limit": 5})).raise_for_status().json()
        briefs = (await client.get("/api/briefs/latest")).raise_for_status().json()
    ws_url = base.replace("http", "ws", 1) + "/ws/stream"
    async with websockets.connect(ws_url) as ws:
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
    if not stories["stories"] or first["type"] != "snapshot":
        raise RuntimeError("smoke check: no stories or no snapshot on the websocket")
    return {
        "healthz": health,
        "stories": len(stories["stories"]),
        "top": stories["stories"][0]["label"],
        "market_brief": bool(briefs["market"]),
        "ws_first": first["type"],
    }


@app.command()
def demo(
    speed: Annotated[float, typer.Option(help="Replay speed; 0 = as fast as possible")] = 300.0,
    fixture: Path = DEFAULT_FIXTURE,
    host: str = "127.0.0.1",
    port: int = 8000,
    db: Path = REPO_ROOT / "var" / "demo.sqlite",
    embedder: Annotated[str | None, typer.Option(help="auto|local|hashing|openai")] = None,
    llm: Annotated[str | None, typer.Option(help="auto|off|openai")] = None,
    exit_when_done: bool = False,
    smoke: Annotated[bool, typer.Option(help="After the replay, check REST + WS and exit")] = False,
    verbose: bool = False,
) -> None:
    """Replay the recorded fixture through the in-memory bus and serve the API. No keys needed."""
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)
    settings = _setup(
        "demo",
        verbose,
        database_url=f"sqlite:///{db}",
        summerand_embedder=embedder,
        summerand_llm=llm,
    )
    asyncio.run(_demo(settings, fixture, speed or None, host, port, exit_when_done or smoke, smoke))


async def _demo(
    settings: Settings,
    fixture: Path,
    speed: float | None,
    host: str,
    port: int,
    exit_when_done: bool,
    smoke: bool,
) -> None:
    from summerand.api.app import create_app
    from summerand.api.ws import Hub
    from summerand.stack import build_stack, run_replay

    stack = build_stack(settings)
    hub = Hub(event_clock=stack.pipeline.clock)
    consumer = asyncio.create_task(hub.consume(stack.bus))
    beat = asyncio.create_task(hub.heartbeat())
    api = create_app(
        stack.store,
        hub,
        settings.watchlist,
        status=stack.status,
        extension_ids=settings.summerand_extension_ids,
    )
    server, serving = await _serve(api, host, port)
    base = f"http://{host}:{port}"
    print(
        f"summerand demo: {base}  (embedder {stack.pipeline.index.gen.embedder_id}, "
        f"llm: {stack.pipeline.briefer.state})",
        flush=True,
    )
    pace = f"{speed:g}x" if speed else "full speed"
    print(
        f"replaying {fixture.name} at {pace}; WebSocket at {base.replace('http', 'ws')}/ws/stream",
        flush=True,
    )

    async def progress() -> None:
        last = -1
        while True:
            await asyncio.sleep(2)
            p = stack.progress
            st = stack.pipeline.status(stack.driver)
            span = (p.get("last_ms") or 0) - (p.get("first_ms") or 0)
            if span > 0 and st["event_ms"]:
                pct = int(100 * (st["event_ms"] - p["first_ms"]) / span)
                if pct // 10 != last // 10:
                    last = pct
                    print(
                        f"  {pct:3d}%  event time {_hhmm(st['event_ms'])}"
                        f"  window {st['window_size']}  clusters {st['clusters']}  k={st['k']}",
                        flush=True,
                    )

    ticker = asyncio.create_task(progress())
    t0 = time.monotonic()
    await run_replay(stack, fixture, speed)
    ticker.cancel()
    await asyncio.sleep(0.2)
    st = stack.status()
    print(
        f"replay done in {time.monotonic() - t0:.0f}s: {st['news']} articles, {st['bars']} bars, "
        f"{st['clusters']} clusters, late dropped {st['late_dropped'] or 0}, llm {st['llm']}",
        flush=True,
    )
    try:
        if smoke:
            print("smoke check:", json.dumps(await smoke_check(base)), flush=True)
        if not exit_when_done:
            print("serving final state; Ctrl-C to stop", flush=True)
            await serving
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await serving
        for t in (consumer, beat):
            t.cancel()


def _hhmm(ts: int | None) -> str:
    from summerand.rank.loop import hhmm

    return hhmm(ts) + " UTC" if ts else "-"


@app.command()
def live(
    host: str = "127.0.0.1",
    port: int = 8000,
    db: Path = REPO_ROOT / "var" / "live.sqlite",
    verbose: bool = False,
) -> None:
    """RSS feeds + Coinbase ticks -> pipeline -> API in one process (needs network, no Kafka)."""
    settings = _setup("live", verbose, database_url=f"sqlite:///{db}")
    asyncio.run(_live(settings, host, port))


async def _live(settings: Settings, host: str, port: int) -> None:
    import httpx

    from summerand.api.app import create_app
    from summerand.api.ws import Hub
    from summerand.etl.etl_service import run_etl
    from summerand.ingest.rss_spider import RssSpider, run_rss
    from summerand.market.coinbase_ws import stream_coinbase
    from summerand.pipeline import run_pipeline
    from summerand.stack import build_stack

    stack = build_stack(settings, live=True)
    stack.mode = "live"
    hub = Hub(event_clock=stack.pipeline.clock)
    inputs = [T.CLEAN_NEWS, T.TICKS_CRYPTO]
    raw_sub = stack.bus.subscribe(T.RAW_NEWS, "etl")
    subs = {t: stack.bus.subscribe(t) for t in inputs}
    tasks = [
        asyncio.create_task(hub.consume(stack.bus)),
        asyncio.create_task(hub.heartbeat()),
        asyncio.create_task(run_etl(stack.bus, stack.etl, sub=raw_sub)),
        asyncio.create_task(
            run_pipeline(
                stack.bus,
                stack.pipeline,
                inputs,
                live=True,
                subs=subs,
                on_driver=lambda d: setattr(stack, "driver", d),
            )
        ),
    ]
    async with httpx.AsyncClient(follow_redirects=True) as client:
        spider = RssSpider(settings.sources.enabled_feeds, client)
        tasks.append(asyncio.create_task(run_rss(stack.bus, spider, settings.sources.poll_seconds)))
        tasks.append(asyncio.create_task(stream_coinbase(stack.bus, settings.coinbase_ws_url)))
        api = create_app(
            stack.store,
            hub,
            settings.watchlist,
            status=stack.status,
            extension_ids=settings.summerand_extension_ids,
        )
        _, serving = await _serve(api, host, port)
        print(
            f"summerand live: http://{host}:{port}"
            f"  (embedder {stack.pipeline.index.gen.embedder_id})"
        )
        await serving
    for t in tasks:
        t.cancel()


# compose services -----------------------------------------------------------------------------


async def _kafka(settings: Settings):
    from summerand.bus.kafka import KafkaBus

    bus = KafkaBus(settings.kafka_broker, prefix=settings.summerand_topic_prefix)
    for attempt in range(30):
        try:
            await bus.start()
            return bus
        except Exception as exc:  # broker not up yet
            log.info("waiting for kafka at %s (%s)", settings.kafka_broker, type(exc).__name__)
            await asyncio.sleep(min(2 + attempt, 10))
    raise RuntimeError(f"kafka at {settings.kafka_broker} never came up")


@ingest_app.command("rss")
def ingest_rss() -> None:
    settings = _setup("ingest-rss")

    async def run() -> None:
        import httpx

        from summerand.ingest.rss_spider import RssSpider, run_rss

        bus = await _kafka(settings)
        async with httpx.AsyncClient(follow_redirects=True) as client:
            await run_rss(
                bus,
                RssSpider(settings.sources.enabled_feeds, client),
                settings.sources.poll_seconds,
            )

    asyncio.run(run())


@ingest_app.command("coinbase")
def ingest_coinbase() -> None:
    settings = _setup("ingest-coinbase")

    async def run() -> None:
        from summerand.market.coinbase_ws import stream_coinbase

        await stream_coinbase(await _kafka(settings), settings.coinbase_ws_url)

    asyncio.run(run())


@ingest_app.command("polygon")
def ingest_polygon() -> None:
    settings = _setup("ingest-polygon")
    key = settings.secret("polygon_api_key")
    if not key:
        log.error("ingest-polygon disabled: POLYGON_API_KEY is not set")
        raise typer.Exit(0)

    async def run() -> None:
        from summerand.market.polygon_ws import stream_polygon

        await stream_polygon(await _kafka(settings), key, settings.polygon_ws_url)

    _run_feed("ingest-polygon", run)


@ingest_app.command("cryptopanic")
def ingest_cryptopanic() -> None:
    settings = _setup("ingest-cryptopanic")
    token = settings.secret("cryptopanic_token")
    if not token:
        log.error("ingest-cryptopanic disabled: CRYPTOPANIC_TOKEN is not set")
        raise typer.Exit(0)

    async def run() -> None:
        import httpx

        from summerand.ingest.cryptopanic import run_cryptopanic

        url = settings.cryptopanic_url or settings.sources.cryptopanic_url
        async with httpx.AsyncClient() as client:
            await run_cryptopanic(
                await _kafka(settings),
                client,
                url,
                token,
                settings.sources.cryptopanic_poll_seconds,
            )

    _run_feed("ingest-cryptopanic", run)


@app.command()
def etl(
    once: Annotated[bool, typer.Option(help="exit at end-of-stream (replay runs)")] = False,
) -> None:
    """raw_news -> clean_news on Kafka."""
    settings = _setup("etl")

    async def run() -> None:
        from summerand.etl.etl_service import Etl, run_etl

        bus = await _kafka(settings)
        await run_etl(bus, Etl(settings.watchlist), stop_on_eos=once)
        await bus.close()

    asyncio.run(run())


@app.command()
def pipeline(
    inputs: Annotated[
        str | None,
        typer.Option(help="topics; default live clean_news,ticks_crypto, replay clean_news,bars"),
    ] = None,
    live_mode: Annotated[bool, typer.Option("--live/--replay")] = True,
) -> None:
    """Consume clean news and prices from Kafka, write the store, publish rankings and briefs.

    State is rebuilt from the retained topics on every start (no committed offsets; retention is
    set per topic in compose), so a restart replays the recent log instead of losing the window.
    While catching up, LLM briefs and WebSocket pushes are suppressed."""
    settings = _setup("pipeline")

    async def run() -> None:
        from summerand.brief.generate import Briefer
        from summerand.nlp.cluster import WindowClusterer
        from summerand.nlp.embed import build_embedders
        from summerand.nlp.index import EmbeddingIndex
        from summerand.pipeline import Pipeline, run_pipeline
        from summerand.stack import build_llm
        from summerand.store.repo import Store

        bus = await _kafka(settings)
        store = Store(settings.database_url)
        chain = build_embedders(
            settings.summerand_embedder,
            settings.secret("openai_api_key"),
            settings.summerand_models_dir,
            settings.summerand_embed_model,
        )
        pipe = Pipeline(
            store=store,
            bus=bus,
            index=EmbeddingIndex(chain, cache=store),
            sources=settings.sources,
            briefer=Briefer(build_llm(settings)),
            clusterer=WindowClusterer(first_id=store.next_cluster_number()),
            live=live_mode,
        )
        if live_mode:
            n = store.retire_open_clusters(time.time_ns() // 1_000_000)
            if n:
                log.info("retired %d clusters left open by a previous run", n)
        default = "clean_news,ticks_crypto" if live_mode else "clean_news,bars"
        topics = (inputs or default).split(",")
        log.info("pipeline inputs=%s embedder=%s", topics, chain[0].id)
        driver = await run_pipeline(bus, pipe, topics, live=live_mode)
        log.info("pipeline finished: %s", json.dumps(pipe.status(driver)))
        await bus.close()

    asyncio.run(run())


@app.command()
def api(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Serve REST + WebSocket from the store, pushing rankings and briefs consumed from Kafka."""
    settings = _setup("api")

    async def run() -> None:
        from summerand.api.app import create_app
        from summerand.api.ws import Hub
        from summerand.store.repo import Store

        bus = await _kafka(settings)
        store = Store(settings.database_url)
        hub = Hub()
        tasks = [
            asyncio.create_task(
                hub.consume(bus, group=f"api-{uuid.uuid4().hex[:8]}", from_beginning=False)
            ),
            asyncio.create_task(hub.heartbeat()),
        ]
        _, serving = await _serve(
            create_app(
                store,
                hub,
                settings.watchlist,
                status=lambda: {"mode": "compose"},
                extension_ids=settings.summerand_extension_ids,
            ),
            host,
            port,
        )
        await serving
        for t in tasks:
            t.cancel()

    asyncio.run(run())


@app.command()
def replay(fixture: Path = DEFAULT_FIXTURE, speed: float = 300.0) -> None:
    """Publish a fixture to Kafka (compose `demo-kafka` profile)."""
    settings = _setup("replay")

    async def run() -> None:
        from summerand.ingest.replay import replay as do_replay

        bus = await _kafka(settings)
        n = await do_replay(bus, fixture, speed=speed or None)
        await bus.close()
        log.info("replayed %d records", n)

    asyncio.run(run())


@app.command()
def smoke(
    base: str = "http://127.0.0.1:8000",
    wait: Annotated[float, typer.Option(help="seconds to wait for a first ranking")] = 0,
) -> None:
    """Check a running server's REST + WebSocket endpoints."""
    _setup("smoke")

    async def run() -> dict[str, object]:
        import httpx

        deadline = time.monotonic() + wait
        while True:
            try:
                async with httpx.AsyncClient(base_url=base, timeout=5) as client:
                    if (await client.get("/healthz")).json().get("event_ms"):
                        break
            except (httpx.HTTPError, ValueError):
                pass
            if time.monotonic() > deadline:
                break
            await asyncio.sleep(2)
        return await smoke_check(base)

    print(json.dumps(asyncio.run(run())))


@app.command("egress-check")
def egress_check(expect: Annotated[str, typer.Option(help="blocked|open")] = "blocked") -> None:
    """Probe external hosts. Exit 0 if the result matches --expect."""
    from summerand.egress import TARGETS, open_targets

    reachable = open_targets()
    print(json.dumps({"targets": [f"{h}:{p}" for h, p in TARGETS], "reachable": reachable}))
    ok = (not reachable) if expect == "blocked" else bool(reachable)
    raise typer.Exit(0 if ok else 1)


@app.command()
def digest(fixture: Path = DEFAULT_FIXTURE, speed: float = 0.0, embedder: str = "hashing") -> None:
    """Replay a fixture and print the state digest (for determinism checks)."""
    settings = _setup(
        "digest",
        database_url="sqlite:///:memory:",
        summerand_embedder=embedder,
        summerand_llm="off",
    )

    async def run() -> str:
        from summerand.stack import build_stack, run_replay

        stack = build_stack(settings)
        await run_replay(stack, fixture, speed or None)
        return stack.pipeline.digest_hash()

    print(asyncio.run(run()))


if __name__ == "__main__":
    app()

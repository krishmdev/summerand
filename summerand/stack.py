"""Assemble the in-process stack used by `summerand demo`, `summerand live` and the tests."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from summerand.brief.generate import LLM, Briefer, OpenAIChat
from summerand.bus import InMemoryBus
from summerand.bus import topics as T
from summerand.config import Settings
from summerand.driver import EventTimeDriver
from summerand.etl.etl_service import Etl, run_etl
from summerand.ingest.replay import replay
from summerand.nlp.cluster import WindowClusterer
from summerand.nlp.embed import Embedder, build_embedders
from summerand.nlp.index import EmbeddingIndex, RetryPolicy
from summerand.pipeline import Pipeline, run_pipeline
from summerand.store.repo import Store

log = logging.getLogger(__name__)


def build_llm(settings: Settings) -> LLM | None:
    key = settings.secret("openai_api_key")
    if settings.summerand_llm == "off" or not key:
        if settings.summerand_llm == "openai" and not key:
            raise RuntimeError("SUMMERAND_LLM=openai but OPENAI_API_KEY is not set")
        return None
    return OpenAIChat(key, settings.summerand_brief_model)


@dataclass
class Stack:
    settings: Settings
    bus: InMemoryBus
    store: Store
    pipeline: Pipeline
    etl: Etl
    driver: EventTimeDriver | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    mode: str = "demo"

    def status(self) -> dict[str, Any]:
        return {"mode": self.mode, "replay": self.progress, **self.pipeline.status(self.driver)}


def build_stack(
    settings: Settings,
    *,
    chain: list[Embedder] | None = None,
    llm: LLM | str | None = "auto",
    store: Store | None = None,
    retry: RetryPolicy | None = None,
    live: bool = False,
) -> Stack:
    bus = InMemoryBus()
    store = store or Store(settings.database_url)
    if chain is None:
        chain = build_embedders(
            settings.summerand_embedder,
            settings.secret("openai_api_key"),
            settings.summerand_models_dir,
            settings.summerand_embed_model,
        )
    index = EmbeddingIndex(chain, retry=retry, cache=store)
    briefer = Briefer(build_llm(settings) if llm == "auto" else llm)
    pipeline = Pipeline(
        store=store,
        bus=bus,
        index=index,
        sources=settings.sources,
        briefer=briefer,
        clusterer=WindowClusterer(first_id=store.next_cluster_number()),
        live=live,
    )
    log.info("embedder chain: %s", " -> ".join(e.id for e in chain))
    return Stack(settings, bus, store, pipeline, Etl(settings.watchlist))


async def run_replay(stack: Stack, fixture: Path, speed: float | None) -> EventTimeDriver:
    """Replay a fixture through bus -> ETL -> pipeline and wait until the pipeline has drained."""
    inputs = [T.CLEAN_NEWS, T.BARS]
    raw_sub = stack.bus.subscribe(T.RAW_NEWS, "etl")
    subs = {t: stack.bus.subscribe(t, None) for t in inputs}

    def keep(driver: EventTimeDriver) -> None:
        stack.driver = driver

    etl = asyncio.create_task(run_etl(stack.bus, stack.etl, sub=raw_sub, stop_on_eos=True))
    pipe = asyncio.create_task(
        run_pipeline(stack.bus, stack.pipeline, inputs, subs=subs, on_driver=keep)
    )
    await replay(stack.bus, fixture, speed=speed, progress=stack.progress)
    await etl
    driver = await pipe
    stack.progress["done"] = True
    return driver

"""Vectors for the active window, one embedding backend per generation.

If the active backend keeps failing, the index re-embeds the whole window with the next backend in
the chain and swaps to that new generation in one step. Until then the old generation stays
untouched (and the pipeline serves its last good ranking, marked stale), so vectors from two
backends never sit in the same index.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from summerand.nlp.embed import Embedder, EmbeddingError

log = logging.getLogger(__name__)


@dataclass
class RetryPolicy:
    attempts: int = 3
    base_delay_s: float = 0.5
    max_delay_s: float = 8.0
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


class VectorCache(Protocol):
    def get_vectors(self, embedder_id: str, ids: list[str]) -> dict[str, np.ndarray]: ...

    def put_vectors(self, embedder_id: str, vectors: dict[str, np.ndarray]) -> None: ...


@dataclass
class Generation:
    number: int
    embedder_id: str
    dim: int
    vectors: dict[str, np.ndarray] = field(default_factory=dict)


class EmbeddingIndex:
    def __init__(
        self,
        chain: list[Embedder],
        retry: RetryPolicy | None = None,
        max_failures: int = 3,
        cache: VectorCache | None = None,
    ) -> None:
        if not chain:
            raise ValueError("need at least one embedder")
        self._chain = list(chain)
        self.embedder = self._chain.pop(0)
        self.retry = retry or RetryPolicy()
        self.max_failures = max_failures
        self.cache = cache
        self.gen = Generation(1, self.embedder.id, self.embedder.dim)
        self._texts: dict[str, str] = {}
        self._pending: dict[str, None] = {}
        self.failures = 0
        self.degraded = False
        self.switches: list[dict] = []

    @property
    def generation(self) -> int:
        return self.gen.number

    @property
    def pending(self) -> list[str]:
        return list(self._pending)

    def add(self, item_id: str, text: str) -> None:
        if item_id in self._texts:
            return
        self._texts[item_id] = text
        self._pending[item_id] = None

    def evict(self, keep: set[str]) -> None:
        for item_id in [i for i in self._texts if i not in keep]:
            self._texts.pop(item_id, None)
            self._pending.pop(item_id, None)
            self.gen.vectors.pop(item_id, None)

    def matrix(self, ids: list[str]) -> tuple[list[str], np.ndarray]:
        present = [i for i in ids if i in self.gen.vectors]
        if not present:
            return [], np.zeros((0, self.gen.dim), dtype=np.float32)
        return present, np.vstack([self.gen.vectors[i] for i in present])

    def check(self) -> None:
        for item_id, vec in self.gen.vectors.items():
            if vec.shape != (self.gen.dim,):
                raise AssertionError(
                    f"{item_id} has shape {vec.shape} in generation {self.gen.number}"
                )

    async def _embed(self, embedder: Embedder, ids: list[str]) -> dict[str, np.ndarray]:
        found = self.cache.get_vectors(embedder.id, ids) if self.cache else {}
        missing = [i for i in ids if i not in found]
        if missing:
            texts = [self._texts[i] for i in missing]
            last: Exception | None = None
            for attempt in range(self.retry.attempts):
                try:
                    vecs = await embedder.embed(texts)
                    if vecs.shape != (len(missing), embedder.dim):
                        raise EmbeddingError(f"{embedder.id} returned shape {vecs.shape}")
                    break
                except Exception as exc:  # any backend failure counts, not only EmbeddingError
                    last = exc
                    if attempt + 1 < self.retry.attempts:
                        delay = min(self.retry.base_delay_s * 2**attempt, self.retry.max_delay_s)
                        await self.retry.sleep(delay)
            else:
                raise EmbeddingError(
                    f"{embedder.id} failed after {self.retry.attempts} tries"
                ) from last
            fresh = {i: vecs[j] for j, i in enumerate(missing)}
            if self.cache:
                self.cache.put_vectors(embedder.id, fresh)
            found.update(fresh)
        return found

    async def sync(self) -> str:
        """Embed pending items. Returns "idle", "ok", "degraded" or "regenerated"."""
        if not self._pending:
            return "idle"
        ids = list(self._pending)
        try:
            vecs = await self._embed(self.embedder, ids)
        except EmbeddingError as exc:
            self.failures += 1
            self.degraded = True
            log.warning("%s (failure %d/%d)", exc, self.failures, self.max_failures)
            if self.failures >= self.max_failures and self._chain:
                return "regenerated" if await self._regenerate() else "degraded"
            return "degraded"
        self.gen.vectors.update(vecs)
        self._pending.clear()
        self.failures = 0
        self.degraded = False
        return "ok"

    async def _regenerate(self) -> bool:
        while self._chain:
            nxt = self._chain[0]
            ids = sorted(self._texts)
            try:
                vecs = await self._embed(nxt, ids)
            except EmbeddingError as exc:
                log.error("fallback %s also failed: %s", nxt.id, exc)
                self._chain.pop(0)
                continue
            new = Generation(self.gen.number + 1, nxt.id, nxt.dim, vecs)
            self.switches.append(
                {
                    "from": self.gen.embedder_id,
                    "to": nxt.id,
                    "generation": new.number,
                    "items": len(ids),
                }
            )
            log.warning("switched to embedding generation %d (%s)", new.number, nxt.id)
            # The swap: everything the rest of the pipeline reads changes in these assignments,
            # with no await in between.
            self._chain.pop(0)
            self.embedder, self.gen = nxt, new
            self._pending.clear()
            self.failures = 0
            self.degraded = False
            return True
        return False

import numpy as np
import pytest

from summerand.nlp.embed import EmbeddingError, HashingEmbedder
from summerand.nlp.index import EmbeddingIndex, RetryPolicy


class FlakyOpenAI:
    """Pretends to be text-embedding-3-small: works for the first `budget` docs, then fails."""

    id = "openai/fake-3-small/api/1536/test"
    dim = 1536
    outlier_cos = 0.3

    def __init__(self, budget=50):
        self.budget = budget
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        if len(texts) > self.budget:
            raise EmbeddingError("503 service unavailable")
        self.budget -= len(texts)
        rng = np.random.default_rng(len(texts))
        v = rng.standard_normal((len(texts), self.dim)).astype(np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)


async def no_sleep(_):
    return None


async def test_switches_generation_after_repeated_failures():
    primary, fallback = FlakyOpenAI(budget=50), HashingEmbedder(256)
    idx = EmbeddingIndex(
        [primary, fallback], retry=RetryPolicy(attempts=2, sleep=no_sleep), max_failures=3
    )
    for i in range(50):
        idx.add(f"d{i}", f"doc {i}")
    assert await idx.sync() == "ok"
    assert idx.generation == 1 and len(idx.gen.vectors) == 50
    for i in range(50, 80):
        idx.add(f"d{i}", f"doc {i}")
    assert await idx.sync() == "degraded"
    assert await idx.sync() == "degraded"
    assert idx.degraded and idx.generation == 1
    # Still generation 1 and still only primary vectors: nothing half-built leaked in.
    assert {v.shape for v in idx.gen.vectors.values()} == {(1536,)}
    assert await idx.sync() == "regenerated"
    assert idx.generation == 2 and not idx.degraded
    assert idx.gen.embedder_id == fallback.id and idx.embedder is fallback
    assert len(idx.gen.vectors) == 80  # the whole window was re-embedded
    assert {v.shape for v in idx.gen.vectors.values()} == {(256,)}
    idx.check()
    assert idx.switches == [{"from": primary.id, "to": fallback.id, "generation": 2, "items": 80}]


async def test_retries_before_counting_a_failure():
    class Blip(FlakyOpenAI):
        async def embed(self, texts):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError
            return await super().embed(texts)

    sleeps = []

    async def record(d):
        sleeps.append(d)

    idx = EmbeddingIndex([Blip(), HashingEmbedder()], retry=RetryPolicy(attempts=3, sleep=record))
    idx.add("a", "x")
    assert await idx.sync() == "ok"
    assert sleeps == [0.5] and idx.failures == 0


async def test_cache_avoids_reembedding():
    class Cache:
        def __init__(self):
            self.d = {}

        def get_vectors(self, eid, ids):
            return {i: self.d[(eid, i)] for i in ids if (eid, i) in self.d}

        def put_vectors(self, eid, vecs):
            self.d.update({(eid, i): v for i, v in vecs.items()})

    cache, emb = Cache(), FlakyOpenAI(budget=3)
    idx = EmbeddingIndex([emb], cache=cache)
    for i in range(3):
        idx.add(str(i), "t")
    await idx.sync()
    idx2 = EmbeddingIndex([emb], cache=cache)
    for i in range(3):
        idx2.add(str(i), "t")
    assert await idx2.sync() == "ok" and emb.calls == 1


def test_embedder_ids_carry_dim_and_preprocessing():
    e = HashingEmbedder(512)
    provider, model, rev, dim, pp = e.id.split("/")
    assert provider == "hashing" and dim == "512" and len(pp) == 8
    with pytest.raises(ValueError):
        EmbeddingIndex([])

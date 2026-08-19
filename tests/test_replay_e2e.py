import pytest

from summerand.nlp.embed import HashingEmbedder
from summerand.stack import build_stack, run_replay
from tests.conftest import ROOT

TINY = ROOT / "fixtures" / "tiny_feed.jsonl"


async def replay_tiny(settings, speed=None):
    stack = build_stack(settings, chain=[HashingEmbedder()], llm=None)
    await run_replay(stack, TINY, speed)
    return stack


async def test_tiny_replay_produces_ranked_clusters_and_briefs(settings):
    stack = await replay_tiny(settings)
    p = stack.pipeline
    assert stack.driver.late_dropped == {}
    assert stack.etl.dropped_exact + stack.etl.dropped_near == 2
    snap = stack.store.latest_snapshot()
    assert snap is not None and not snap["stale"] and snap["stories"]
    labels = " | ".join(s["label"] for s in snap["stories"][:3])
    assert "solana" in labels and ("ether" in labels or "etf" in labels) and "nvidia" in labels
    top = {s["cluster_id"]: s for s in snap["stories"]}
    sol = next(s for s in top.values() if "solana" in s["label"])
    assert "SOL" in sol["tickers"] and sol["components"]["market"] is not None
    assert any(m["symbol"] == "SOL" and m["pct"] < 0 for m in sol["moves"])
    nv = next(s for s in top.values() if "nvidia" in s["label"])
    assert nv["components"]["market"] is None  # no NVDA bars in the fixture: weights renormalize
    briefs = stack.store.briefs()
    assert any(b["kind"] == "cluster" for b in briefs) and any(
        b["kind"] == "market" for b in briefs
    )
    assert all(b["method"] == "extractive" for b in briefs)
    assert p.scheduler.fired["recluster"] >= 60 and p.scheduler.fired["rank"] >= 120


@pytest.mark.slow
async def test_unpaced_and_300x_replays_are_identical(settings, tmp_path):
    from summerand.config import Settings

    fast = await replay_tiny(settings)
    other = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'paced.sqlite'}",
        summerand_embedder="hashing",
        summerand_llm="off",
    )
    paced = await replay_tiny(other, speed=300)
    a, b = fast.pipeline.digest(), paced.pipeline.digest()
    assert a["clusters"] and a["snapshots"] and a["briefs"]
    assert a["clusters"] == b["clusters"]
    assert a["snapshots"] == b["snapshots"]
    assert a["briefs"] == b["briefs"]
    assert fast.pipeline.digest_hash() == paced.pipeline.digest_hash()


async def test_embedding_outage_serves_stale_then_switches_generation(settings):
    from summerand.nlp.index import RetryPolicy
    from tests.test_embed_index import FlakyOpenAI, no_sleep

    primary, fallback = FlakyOpenAI(budget=12), HashingEmbedder(512)
    stack = build_stack(
        settings, chain=[primary, fallback], llm=None, retry=RetryPolicy(attempts=2, sleep=no_sleep)
    )
    await run_replay(stack, TINY, None)
    p = stack.pipeline
    assert p.stats["regenerations"] == 1 and p.stats["stale_snapshots"] >= 1
    assert p.index.generation == 2 and p.index.gen.embedder_id == fallback.id
    p.index.check()
    state = p.clusterer.state
    assert state.generation == 2
    assert {c.embedder_id for c in state.clusters.values()} == {fallback.id}
    assert {c.centroid.shape for c in state.clusters.values()} == {(512,)}
    snaps = stack.store.snapshots()
    stale = [s for s in snaps if s["stale"]]
    # While embeddings were down the last good ranking was re-served unchanged, flagged stale.
    assert stale and all(s["embedder_id"] == primary.id for s in stale)
    assert all(s["stories"] == snaps[snaps.index(s) - 1]["stories"] for s in stale)
    assert snaps[-1]["stale"] is False and snaps[-1]["embedder_id"] == fallback.id

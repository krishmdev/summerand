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

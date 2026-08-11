import numpy as np

from summerand.clock import HOUR_MS, MINUTE_MS
from summerand.nlp.cluster import WindowClusterer, select_k
from summerand.nlp.embed import HashingEmbedder
from summerand.nlp.label import ctfidf_terms
from tests.topics import headlines


def blobs(n_per=30, dim=16, seed=0):
    rng = np.random.default_rng(seed)
    centers = np.eye(dim)[:3]
    X = np.vstack([c + 0.08 * rng.standard_normal((n_per, dim)) for c in centers])
    return X / np.linalg.norm(X, axis=1, keepdims=True)


def test_silhouette_picks_three_blobs():
    k, scores = select_k(blobs())
    assert k == 3
    assert scores[3] == max(scores.values())


def test_small_windows_get_one_cluster():
    assert select_k(blobs(n_per=3))[0] == 1


async def embed(texts, emb=None):
    emb = emb or HashingEmbedder()
    return emb, await emb.embed(texts)


def fit(clu, now, ids, X, emb, generation=1, select=True):
    return clu.refit(
        now,
        ids,
        X,
        generation=generation,
        embedder_id=emb.id,
        outlier_cos=emb.outlier_cos,
        select=select,
    )


async def test_ids_are_stable_across_refits():
    texts = headlines("solana", 12) + headlines("nvidia", 12) + headlines("fed", 12)
    ids = [f"d{i}" for i in range(len(texts))]
    emb, X = await embed(texts)
    clu = WindowClusterer()
    s1 = fit(clu, 0, ids, X, emb)
    assert s1.k == 3 and len(s1.fresh) == 3
    # One minute later a few articles arrived; nothing else changed.
    more = headlines("solana", 14, seed=0)[12:]
    _, X2 = await embed(texts + more)
    s2 = fit(clu, MINUTE_MS, ids + ["n1", "n2"], X2, emb, select=False)
    assert s2.fresh == [] and s2.retired == []
    assert set(s2.clusters) == set(s1.clusters)
    sol = s2.cluster_of("d0")
    assert {"n1", "n2"} <= set(s2.clusters[sol].members)


async def test_equal_count_replacement_gets_fresh_ids():
    clu = WindowClusterer()
    old = headlines("solana", 12) + headlines("nvidia", 12) + headlines("fed", 12)
    new = headlines("etf", 12) + headlines("tesla", 12) + headlines("hack", 12)
    emb, X_old = await embed(old)
    _, X_new = await embed(new)
    s1 = fit(clu, 0, [f"o{i}" for i in range(36)], X_old, emb)
    s2 = fit(clu, HOUR_MS, [f"n{i}" for i in range(36)], X_new, emb)
    assert s1.k == s2.k == 3
    assert s2.inherited == {}
    assert len(s2.fresh) == 3 and sorted(s2.retired) == sorted(s1.clusters)
    assert not set(s2.clusters) & set(s1.clusters)


async def test_topic_is_replaced_after_window_expiry():
    """Topic A only in hours 0-6, topic B only in hours 6-12, refit on the true 6h window."""
    emb = HashingEmbedder()
    docs = []  # (ts, id, text)
    a_texts, b_texts = (
        headlines("solana", 36) + headlines("fed", 36),
        headlines("etf", 36) + headlines("tesla", 36),
    )
    for i, text in enumerate(a_texts):
        docs.append((i * 5 * MINUTE_MS, f"a{i}", text))
    for i, text in enumerate(b_texts):
        docs.append((6 * HOUR_MS + 30_000 + i * 5 * MINUTE_MS, f"b{i}", text))
    vecs = dict(zip([d[1] for d in docs], await emb.embed([d[2] for d in docs]), strict=True))
    clu = WindowClusterer()
    a_ids: set[str] = set()
    for t in range(10 * MINUTE_MS, 12 * HOUR_MS + 1, 10 * MINUTE_MS):
        window = [d for d in docs if t - 6 * HOUR_MS <= d[0] <= t]
        ids = [d[1] for d in window]
        state = fit(clu, t, ids, np.vstack([vecs[i] for i in ids]), emb, select=(t % HOUR_MS == 0))
        if t <= 6 * HOUR_MS:
            a_ids |= set(state.clusters)
    assert all(not m.startswith("a") for c in state.clusters.values() for m in c.members)
    assert a_ids and a_ids <= set(clu.retired)
    assert not a_ids & set(state.clusters)


async def test_generation_switch_keeps_ids_only_with_document_overlap():
    texts = headlines("solana", 12) + headlines("nvidia", 12) + headlines("fed", 12)
    ids = [f"d{i}" for i in range(36)]
    g1, X1 = await embed(texts, HashingEmbedder(1024))
    g2, X2 = await embed(texts, HashingEmbedder(256))
    clu = WindowClusterer()
    s1 = fit(clu, 0, ids, X1, g1, generation=1)
    s2 = fit(clu, MINUTE_MS, ids, X2, g2, generation=2)
    assert s2.embedder_id == g2.id and all(c.centroid.shape == (256,) for c in s2.clusters.values())
    assert set(s2.clusters) == set(s1.clusters) and s2.fresh == []
    # Next generation over a window where the fed articles are gone and a new topic arrived.
    g3, X3 = await embed(texts[:24] + headlines("hack", 12), HashingEmbedder(512))
    s3 = fit(clu, 2 * MINUTE_MS, ids[:24] + [f"h{i}" for i in range(12)], X3, g3, generation=3)
    fed_id = s2.cluster_of("d30")
    assert fed_id in s3.retired
    assert s3.cluster_of("d0") == s2.cluster_of("d0")
    assert s3.cluster_of("h0") in s3.fresh


def test_ctfidf_labels_pick_distinctive_terms():
    labels = ctfidf_terms(
        {
            "a": ["Solana outage halts blocks", "Solana validators restart after outage"],
            "b": ["Nvidia earnings beat", "Nvidia guidance lifts chip stocks"],
        }
    )
    assert "solana" in " ".join(labels["a"]) and "outage" in " ".join(labels["a"])
    assert "nvidia" in " ".join(labels["b"])

"""K-Means over the active window, refit from scratch on every re-cluster tick.

MiniBatchKMeans.partial_fit keeps cumulative per-center counts, so an old topic would keep pulling
on its center long after its articles left the window. Instead every tick refits on exactly the
articles in [t-6h, t]. When k hasn't changed the fit is also tried warm-started from the previous
centers and the lower-inertia result wins. Every 10 minutes k is re-selected by cosine silhouette.

Stable IDs: Hungarian matching between the previous and new clusters. Within one embedding
generation the assignment runs on centroid cosine, and an assigned pair is rejected unless it also
has cos >= 0.8 and member Jaccard >= 0.2; plain assignment would otherwise hand old IDs to
unrelated new topics whenever the cluster counts are equal. Across generations the centroids live
in different spaces, so matching uses member Jaccard only (>= 0.3). Jaccard is computed over the
articles present in both windows, so arrivals and expiries alone don't break a match.
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linear_sum_assignment
from sklearn.cluster import MiniBatchKMeans
from sklearn.metrics import silhouette_score

log = logging.getLogger(__name__)


@dataclass
class Cluster:
    id: str
    members: list[str]
    centroid: np.ndarray
    embedder_id: str
    generation: int
    born_ms: int


@dataclass
class ClusterState:
    built_ms: int
    generation: int
    embedder_id: str
    k: int
    clusters: dict[str, Cluster]
    outliers: list[str]
    window: frozenset[str]
    inherited: dict[str, str] = field(default_factory=dict)
    fresh: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)

    def cluster_of(self, article_id: str) -> str | None:
        for c in self.clusters.values():
            if article_id in c.members:
                return c.id
        return None


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def _kmeans(k: int, seed: int, init: np.ndarray | str = "k-means++") -> MiniBatchKMeans:
    warm = not isinstance(init, str)
    return MiniBatchKMeans(
        n_clusters=k,
        init=init,
        n_init=1 if warm else 3,
        batch_size=256,
        max_iter=100,
        random_state=seed,
        reassignment_ratio=0.01,
    )


def select_k(
    X: np.ndarray, k_max: int = 30, min_points: int = 10, seed: int = 7
) -> tuple[int, dict[int, float]]:
    """Pick k in [2, min(k_max, n/3)] with the best cosine silhouette. Small windows get k=1."""
    n = len(X)
    hi = min(k_max, n // 3)
    if n < min_points or hi < 2:
        return 1, {}
    scores: dict[int, float] = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for k in range(2, hi + 1):
            labels = _kmeans(k, seed).fit_predict(X)
            if not 2 <= len(set(labels)) <= n - 1:
                continue
            scores[k] = float(
                silhouette_score(
                    X, labels, metric="cosine", sample_size=min(1000, n), random_state=seed
                )
            )
    if not scores:
        return 1, {}
    best = max(scores, key=lambda k: (round(scores[k], 6), -k))
    return best, scores


@dataclass
class _Draft:
    members: list[str]
    centroid: np.ndarray


class WindowClusterer:
    def __init__(
        self,
        k_max: int = 30,
        min_points: int = 10,
        match_cos: float = 0.8,
        match_jaccard: float = 0.2,
        cross_generation_jaccard: float = 0.3,
        seed: int = 7,
    ) -> None:
        self.k_max = k_max
        self.min_points = min_points
        self.match_cos = match_cos
        self.match_jaccard = match_jaccard
        self.cross_generation_jaccard = cross_generation_jaccard
        self.seed = seed
        self.state: ClusterState | None = None
        self.retired: dict[str, int] = {}
        self.k: int | None = None
        self.silhouette: dict[int, float] = {}
        self._centers: np.ndarray | None = None
        self._centers_generation: int | None = None
        self._next_id = 1

    def _new_id(self) -> str:
        cid = f"c{self._next_id:05d}"
        self._next_id += 1
        return cid

    def refit(
        self,
        now_ms: int,
        ids: list[str],
        X: np.ndarray,
        *,
        generation: int,
        embedder_id: str,
        outlier_cos: float,
        select: bool = False,
    ) -> ClusterState:
        n = len(ids)
        drafts: list[_Draft] = []
        outliers: list[str] = []
        k = 0
        if n:
            same_gen = self._centers_generation == generation
            if n < self.min_points:
                k = 1
                labels = np.zeros(n, dtype=int)
                centers = X.mean(axis=0, keepdims=True)
            else:
                if select or self.k is None or not same_gen or self.k == 1:
                    k, self.silhouette = select_k(X, self.k_max, self.min_points, self.seed)
                else:
                    k = max(1, min(self.k, n // 3))
                warm = (
                    same_gen
                    and self._centers is not None
                    and self._centers.shape == (k, X.shape[1])
                )
                if k == 1:
                    labels = np.zeros(n, dtype=int)
                    centers = X.mean(axis=0, keepdims=True)
                else:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        km = _kmeans(k, self.seed).fit(X)
                        if warm:
                            # The warm start keeps centers steady between ticks, but it can also
                            # keep a bad split alive; keep whichever fit has the lower inertia.
                            warm_km = _kmeans(k, self.seed, self._centers).fit(X)
                            if warm_km.inertia_ <= km.inertia_ + 1e-9:
                                km = warm_km
                    labels, centers = km.labels_, km.cluster_centers_
            self.k = k
            self._centers = centers.astype(np.float32)
            self._centers_generation = generation
            for j in range(centers.shape[0]):
                idx = np.flatnonzero(labels == j)
                if not len(idx):
                    continue
                c = _unit(centers[j])
                cos = X[idx] @ c
                keep = idx[cos >= outlier_cos]
                outliers.extend(ids[i] for i in idx[cos < outlier_cos])
                if not len(keep):
                    continue
                drafts.append(
                    _Draft(
                        sorted(ids[i] for i in keep), _unit(X[keep].mean(axis=0)).astype(np.float32)
                    )
                )
        drafts.sort(key=lambda d: (-len(d.members), d.members[0]))
        return self._commit(
            now_ms, drafts, sorted(outliers), frozenset(ids), generation, embedder_id, k
        )

    def _match(
        self, drafts: list[_Draft], window: frozenset[str], generation: int
    ) -> list[str | None]:
        prev = self.state
        if prev is None or not prev.clusters or not drafts:
            return [None] * len(drafts)
        old = list(prev.clusters.values())
        common = prev.window & window

        def jaccard(a: list[str], b: list[str]) -> float:
            sa, sb = set(a) & common, set(b) & common
            union = sa | sb
            return len(sa & sb) / len(union) if union else 0.0

        J = np.array([[jaccard(o.members, d.members) for d in drafts] for o in old])
        if prev.generation == generation:
            C = np.stack([o.centroid for o in old]) @ np.stack([d.centroid for d in drafts]).T
            rows, cols = linear_sum_assignment(-C)
            accepted = [
                (r, c)
                for r, c in zip(rows, cols, strict=True)
                if C[r, c] >= self.match_cos and J[r, c] >= self.match_jaccard
            ]
        else:
            rows, cols = linear_sum_assignment(-J)
            accepted = [
                (r, c)
                for r, c in zip(rows, cols, strict=True)
                if J[r, c] >= self.cross_generation_jaccard
            ]
        out: list[str | None] = [None] * len(drafts)
        for r, c in accepted:
            out[c] = old[r].id
        return out

    def _commit(
        self,
        now_ms: int,
        drafts: list[_Draft],
        outliers: list[str],
        window: frozenset[str],
        generation: int,
        embedder_id: str,
        k: int,
    ) -> ClusterState:
        prev = self.state
        matched = self._match(drafts, window, generation)
        clusters: dict[str, Cluster] = {}
        inherited: dict[str, str] = {}
        fresh: list[str] = []
        for draft, old_id in zip(drafts, matched, strict=True):
            if old_id is not None and prev is not None:
                cid, born = old_id, prev.clusters[old_id].born_ms
                inherited[cid] = cid
            else:
                cid, born = self._new_id(), now_ms
                fresh.append(cid)
            clusters[cid] = Cluster(
                cid, draft.members, draft.centroid, embedder_id, generation, born
            )
        retired = sorted(set(prev.clusters) - set(clusters)) if prev else []
        for cid in retired:
            self.retired[cid] = now_ms
        self.state = ClusterState(
            built_ms=now_ms,
            generation=generation,
            embedder_id=embedder_id,
            k=k,
            clusters=clusters,
            outliers=outliers,
            window=window,
            inherited=inherited,
            fresh=fresh,
            retired=retired,
        )
        return self.state

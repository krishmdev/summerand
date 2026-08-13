"""Turn the current cluster state into a ranked list of stories (runs every 30s of event time)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np

from summerand.clock import HOUR_MS
from summerand.config import SourcesConfig
from summerand.market.bars import PriceBook
from summerand.nlp.cluster import ClusterState
from summerand.rank import impact
from summerand.schemas import CleanNews

SURFACE_TOP = 10
NOVELTY_MEMORY_MS = 24 * HOUR_MS


def hhmm(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%H:%M")


def r6(x: float | None) -> float | None:
    return None if x is None else round(float(x), 6)


def article_ref(a: CleanNews) -> dict[str, Any]:
    return {
        "id": a.id,
        "title": a.title,
        "url": a.url,
        "source": a.source,
        "published_ms": a.published_ms,
    }


def move_fact(m: impact.TickerMove) -> str:
    return f"{m.symbol} {m.pct:+.1f}% since {hhmm(m.since_ms)} UTC, {abs(m.sigmas):.1f}σ"


@dataclass
class Ranker:
    sources: SourcesConfig
    top_n: int = 20
    ema: dict[str, float] = field(default_factory=dict)
    # cluster id -> (last surfaced ms, centroid, generation)
    surfaced: dict[str, tuple[int, np.ndarray, int]] = field(default_factory=dict)

    def rank(
        self,
        now_ms: int,
        state: ClusterState,
        articles: dict[str, CleanNews],
        prices: PriceBook,
        labels: dict[str, str],
        headlines: dict[str, str],
    ) -> list[dict[str, Any]]:
        scored = []
        for cid, cluster in state.clusters.items():
            members = [articles[i] for i in cluster.members if i in articles]
            if not members:
                continue
            pubs = [a.published_ms for a in members]
            n_recent = sum(1 for p in pubs if p > now_ms - impact.DELTA_MS)
            srcs = sorted({a.source for a in members})
            priors = [self.sources.credibility(s) for s in srcs]
            tickers = impact.priced_tickers([a.tickers for a in members], prices)
            market, moves = impact.market_move(prices, tickers, pubs, now_ms)
            others = [
                cent
                for oid, (ts, cent, gen) in self.surfaced.items()
                if oid != cid and gen == state.generation and now_ms - ts <= NOVELTY_MEMORY_MS
            ]
            comps = {
                "velocity": impact.velocity(n_recent, len(pubs) - n_recent),
                "credibility": impact.credibility(priors),
                "recency": impact.recency(now_ms, max(pubs)),
                "market": market,
                "novelty": impact.novelty(cluster.centroid, others),
            }
            raw = impact.combine(comps)
            score = impact.ema(self.ema.get(cid), raw)
            self.ema[cid] = score
            if not impact.eligible(len(members), max(priors), market):
                continue
            counts = Counter(t for a in members for t in set(a.tickers))
            recent = sorted(members, key=lambda a: (-a.published_ms, a.id))
            head_id = headlines.get(cid)
            head = articles.get(head_id) if head_id else None
            scored.append(
                {
                    "cluster_id": cid,
                    "score": r6(score),
                    "raw_score": r6(raw),
                    "components": {k: r6(v) for k, v in comps.items()},
                    "label": labels.get(cid, ""),
                    "headline": article_ref(head or recent[0]),
                    "size": len(members),
                    "sources": srcs,
                    "tickers": [t for t, _ in sorted(counts.items(), key=lambda x: (-x[1], x[0]))],
                    "moves": [
                        {
                            "symbol": m.symbol,
                            "pct": r6(m.pct),
                            "sigmas": r6(m.sigmas),
                            "since_ms": m.since_ms,
                            "text": move_fact(m),
                        }
                        for m in moves
                    ],
                    "items": [article_ref(a) for a in recent[:5]],
                    "first_ms": min(pubs),
                    "last_ms": max(pubs),
                    "born_ms": cluster.born_ms,
                }
            )
        for cid in [c for c in self.ema if c not in state.clusters]:
            del self.ema[cid]
        scored.sort(key=lambda s: (-s["score"], s["cluster_id"]))
        stories = scored[: self.top_n]
        for i, s in enumerate(stories, 1):
            s["rank"] = i
        for s in stories[:SURFACE_TOP]:
            c = state.clusters[s["cluster_id"]]
            self.surfaced[c.id] = (now_ms, c.centroid, state.generation)
        for oid in [
            o for o, (ts, _, _) in self.surfaced.items() if now_ms - ts > NOVELTY_MEMORY_MS
        ]:
            del self.surfaced[oid]
        return stories

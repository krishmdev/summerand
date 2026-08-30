"""All database access. The pipeline writes, the API reads; in compose they are separate processes
sharing Postgres, in the demo they share one SQLite file."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

import numpy as np
from sqlalchemy import Engine, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from summerand.schemas import Bar, CleanNews
from summerand.store.db import make_engine
from summerand.store.models import (
    Article,
    ArticleTicker,
    BarRow,
    Base,
    BriefRow,
    ClusterRow,
    Embedding,
    Ranking,
    Snapshot,
)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Store:
    def __init__(self, url_or_engine: str | Engine) -> None:
        self.engine = (
            make_engine(url_or_engine) if isinstance(url_or_engine, str) else url_or_engine
        )
        Base.metadata.create_all(self.engine)
        self._dialect = self.engine.dialect.name

    def _upsert(
        self, s: Session, model: type, rows: list[dict], keys: list[str], update: bool
    ) -> None:
        if not rows:
            return
        insert = pg_insert if self._dialect == "postgresql" else sqlite_insert
        for i in range(0, len(rows), 500):
            stmt = insert(model).values(rows[i : i + 500])
            if update:
                cols = {c: stmt.excluded[c] for c in rows[0] if c not in keys}
                stmt = stmt.on_conflict_do_update(index_elements=keys, set_=cols)
            else:
                stmt = stmt.on_conflict_do_nothing(index_elements=keys)
            s.execute(stmt)

    # writes ------------------------------------------------------------------------------------

    def save_articles(self, items: Iterable[CleanNews]) -> None:
        items = list(items)
        with Session(self.engine) as s, s.begin():
            self._upsert(
                s,
                Article,
                [
                    {
                        "id": a.id,
                        "source": a.source,
                        "title": a.title,
                        "url": a.url,
                        "summary": a.summary,
                        "published_ms": a.published_ms,
                        "observed_ms": a.observed_ms,
                        "tickers": a.tickers,
                    }
                    for a in items
                ],
                ["id"],
                update=False,
            )
            self._upsert(
                s,
                ArticleTicker,
                [{"article_id": a.id, "symbol": t} for a in items for t in a.tickers],
                ["article_id", "symbol"],
                update=False,
            )

    def get_vectors(self, embedder_id: str, ids: list[str]) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        with Session(self.engine) as s:
            for i in range(0, len(ids), 500):
                rows = s.execute(
                    select(Embedding).where(
                        Embedding.embedder_id == embedder_id,
                        Embedding.article_id.in_(ids[i : i + 500]),
                    )
                ).scalars()
                for r in rows:
                    out[r.article_id] = np.frombuffer(r.vector, dtype=np.float32).copy()
        return out

    def put_vectors(self, embedder_id: str, vectors: dict[str, np.ndarray]) -> None:
        with Session(self.engine) as s, s.begin():
            self._upsert(
                s,
                Embedding,
                [
                    {
                        "article_id": k,
                        "embedder_id": embedder_id,
                        "dim": int(v.shape[0]),
                        "vector": np.asarray(v, dtype=np.float32).tobytes(),
                    }
                    for k, v in vectors.items()
                ],
                ["article_id", "embedder_id"],
                update=True,
            )

    def save_clusters(self, rows: list[dict[str, Any]], retired: dict[str, int]) -> None:
        with Session(self.engine) as s, s.begin():
            self._upsert(s, ClusterRow, rows, ["id"], update=True)
            for cid, ts in retired.items():
                row = s.get(ClusterRow, cid)
                if row is not None and row.retired_ms is None:
                    row.retired_ms = ts

    def retire_open_clusters(self, ts_ms: int) -> int:
        """At pipeline start: clusters left open by a previous run are not live any more."""
        with Session(self.engine) as s, s.begin():
            rows = (
                s.execute(select(ClusterRow).where(ClusterRow.retired_ms.is_(None))).scalars().all()
            )
            for row in rows:
                row.retired_ms = ts_ms
            return len(rows)

    def save_snapshot(self, snap: dict[str, Any]) -> None:
        with Session(self.engine) as s, s.begin():
            # A rebuilt pipeline may rewrite a snapshot time with a different story set; replace
            # the rows instead of merging old and new rankings.
            s.execute(delete(Ranking).where(Ranking.snapshot_ms == snap["ts_ms"]))
            self._upsert(
                s,
                Snapshot,
                [{"ts_ms": snap["ts_ms"], "stale": snap["stale"], "payload": snap}],
                ["ts_ms"],
                update=True,
            )
            self._upsert(
                s,
                Ranking,
                [
                    {
                        "snapshot_ms": snap["ts_ms"],
                        "cluster_id": st["cluster_id"],
                        "rank": st["rank"],
                        "score": st["score"],
                        "components": st["components"],
                        "stale": snap["stale"],
                    }
                    for st in snap["stories"]
                ],
                ["snapshot_ms", "cluster_id"],
                update=True,
            )

    def save_brief(self, brief: dict[str, Any]) -> None:
        with Session(self.engine) as s, s.begin():
            s.add(
                BriefRow(
                    ts_ms=brief["ts_ms"],
                    kind=brief["kind"],
                    cluster_id=brief.get("cluster_id"),
                    membership_hash=brief.get("membership_hash", ""),
                    method=brief["method"],
                    text=brief["text"],
                    text_hash=text_hash(brief["text"]),
                    payload=brief,
                )
            )

    def save_bars(self, bars: list[Bar]) -> None:
        with Session(self.engine) as s, s.begin():
            self._upsert(
                s,
                BarRow,
                [
                    {
                        "symbol": b.symbol,
                        "ts_ms": b.ts_ms,
                        "venue": b.venue,
                        "open": b.open,
                        "high": b.high,
                        "low": b.low,
                        "close": b.close,
                        "volume": b.volume,
                    }
                    for b in bars
                ],
                ["symbol", "ts_ms"],
                update=True,
            )

    def reset(self) -> None:
        with Session(self.engine) as s, s.begin():
            for model in (Article, ArticleTicker, ClusterRow, Ranking, Snapshot, BriefRow, BarRow):
                s.execute(delete(model))

    # reads -------------------------------------------------------------------------------------

    def next_cluster_number(self) -> int:
        with Session(self.engine) as s:
            ids = s.execute(select(ClusterRow.id)).scalars().all()
        nums = [int(i[1:]) for i in ids if i[:1] == "c" and i[1:].isdigit()]
        return max(nums, default=0) + 1

    def latest_snapshot(self) -> dict[str, Any] | None:
        with Session(self.engine) as s:
            row = s.execute(select(Snapshot).order_by(Snapshot.ts_ms.desc()).limit(1)).scalar()
            return row.payload if row else None

    def snapshots(self) -> list[dict[str, Any]]:
        with Session(self.engine) as s:
            return [
                r.payload for r in s.execute(select(Snapshot).order_by(Snapshot.ts_ms)).scalars()
            ]

    def latest_market_brief(self) -> dict[str, Any] | None:
        with Session(self.engine) as s:
            row = s.execute(
                select(BriefRow)
                .where(BriefRow.kind == "market")
                .order_by(BriefRow.id.desc())
                .limit(1)
            ).scalar()
            return row.payload if row else None

    def briefs(self, kind: str | None = None) -> list[dict[str, Any]]:
        with Session(self.engine) as s:
            q = select(BriefRow).order_by(BriefRow.id)
            if kind:
                q = q.where(BriefRow.kind == kind)
            return [r.payload | {"text_hash": r.text_hash} for r in s.execute(q).scalars()]

    def cluster(self, cluster_id: str) -> dict[str, Any] | None:
        with Session(self.engine) as s:
            row = s.get(ClusterRow, cluster_id)
            if row is None:
                return None
            arts = s.execute(select(Article).where(Article.id.in_(row.members))).scalars()
            articles = sorted((_article(a) for a in arts), key=lambda a: -a["published_ms"])
            brief = s.execute(
                select(BriefRow)
                .where(BriefRow.cluster_id == cluster_id)
                .order_by(BriefRow.id.desc())
                .limit(1)
            ).scalar()
            return {
                "id": row.id,
                "label": row.label,
                "generation": row.generation,
                "embedder_id": row.embedder_id,
                "size": row.size,
                "born_ms": row.born_ms,
                "updated_ms": row.updated_ms,
                "retired_ms": row.retired_ms,
                "articles": articles,
                "brief": brief.payload if brief else None,
            }

    def articles(self, ticker: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with Session(self.engine) as s:
            q = select(Article).order_by(Article.published_ms.desc()).limit(limit)
            if ticker:
                q = q.join(ArticleTicker, ArticleTicker.article_id == Article.id).where(
                    ArticleTicker.symbol == ticker.upper()
                )
            return [_article(a) for a in s.execute(q).scalars()]

    def bars(self, symbol: str, since_ms: int = 0, limit: int = 2000) -> list[dict[str, Any]]:
        with Session(self.engine) as s:
            q = (
                select(BarRow)
                .where(BarRow.symbol == symbol.upper(), BarRow.ts_ms >= since_ms)
                .order_by(BarRow.ts_ms.desc())
                .limit(limit)
            )
            rows = list(s.execute(q).scalars())
        return [
            {
                "ts_ms": r.ts_ms,
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "volume": r.volume,
                "venue": r.venue,
            }
            for r in reversed(rows)
        ]


def _article(a: Article) -> dict[str, Any]:
    return {
        "id": a.id,
        "source": a.source,
        "title": a.title,
        "url": a.url,
        "summary": a.summary,
        "published_ms": a.published_ms,
        "tickers": a.tickers,
    }

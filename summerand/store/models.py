from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, Float, Index, Integer, LargeBinary, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Article(Base):
    __tablename__ = "articles"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    published_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    observed_ms: Mapped[int] = mapped_column(BigInteger)
    tickers: Mapped[list[str]] = mapped_column(JSON, default=list)


class ArticleTicker(Base):
    __tablename__ = "article_tickers"
    article_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), primary_key=True, index=True)


class Embedding(Base):
    __tablename__ = "embeddings"
    article_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    embedder_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    dim: Mapped[int] = mapped_column(Integer)
    vector: Mapped[bytes] = mapped_column(LargeBinary)


class ClusterRow(Base):
    __tablename__ = "clusters"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer)
    embedder_id: Mapped[str] = mapped_column(String(160))
    label: Mapped[str] = mapped_column(Text, default="")
    headline_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size: Mapped[int] = mapped_column(Integer, default=0)
    born_ms: Mapped[int] = mapped_column(BigInteger)
    updated_ms: Mapped[int] = mapped_column(BigInteger)
    retired_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    members: Mapped[list[str]] = mapped_column(JSON, default=list)


class Ranking(Base):
    __tablename__ = "rankings"
    snapshot_ms: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cluster_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    components: Mapped[dict[str, Any]] = mapped_column(JSON)
    stale: Mapped[bool] = mapped_column(Boolean, default=False)


class Snapshot(Base):
    __tablename__ = "snapshots"
    ts_ms: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class BriefRow(Base):
    __tablename__ = "briefs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(16))
    cluster_id: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    membership_hash: Mapped[str] = mapped_column(String(64), default="")
    method: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class BarRow(Base):
    __tablename__ = "bars"
    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    venue: Mapped[str] = mapped_column(String(16))
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)


Index("ix_briefs_kind_ts", BriefRow.kind, BriefRow.ts_ms)

"""Embedding backends. Each one has an `id` of the form provider/model/revision/dim/pp-hash, and
vectors from different ids are never compared or mixed (see nlp/index.py)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Protocol

import numpy as np

from summerand.schemas import CleanNews

log = logging.getLogger(__name__)

PREPROCESS = "title. summary[:300] | v1"
PP_HASH = hashlib.sha256(PREPROCESS.encode()).hexdigest()[:8]

MINILM_REPO = "sentence-transformers/all-MiniLM-L6-v2"


def embed_text(item: CleanNews) -> str:
    if item.summary:
        return f"{item.title}. {item.summary[:300]}"
    return item.title


def l2_normalize(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return x / norms


class EmbeddingError(RuntimeError):
    pass


class Embedder(Protocol):
    id: str
    dim: int
    # Members whose cosine to their centroid falls below this are treated as outliers. Bag-of-words
    # vectors have much lower cosines between related texts than neural embeddings do.
    outlier_cos: float

    async def embed(self, texts: list[str]) -> np.ndarray: ...


class HashingEmbedder:
    """Hashed word 1-2 gram counts. No model and fully deterministic; used by tests and as the
    last fallback."""

    outlier_cos = 0.12

    def __init__(self, dim: int = 1024) -> None:
        from sklearn.feature_extraction.text import HashingVectorizer

        self.dim = dim
        self.id = f"hashing/sklearn-hv/v1/{dim}/{PP_HASH}"
        self._vec = HashingVectorizer(
            n_features=dim,
            ngram_range=(1, 2),
            alternate_sign=False,
            norm="l2",
            stop_words="english",
            lowercase=True,
        )

    async def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return l2_normalize(self._vec.transform(texts).toarray())


def read_models_lock(models_dir: Path) -> dict:
    lock = models_dir.parent / "models.lock"
    if not lock.exists():
        return {}
    return json.loads(lock.read_text())


class LocalEmbedder:
    """all-MiniLM-L6-v2 loaded from the project-local .models/ cache at the pinned revision."""

    outlier_cos = 0.30

    def __init__(self, models_dir: Path, repo: str = MINILM_REPO) -> None:
        from sentence_transformers import SentenceTransformer

        spec = read_models_lock(models_dir).get("models", {}).get(repo)
        if not spec:
            raise EmbeddingError(f"{repo} is not in models.lock; run `make models`")
        self.revision = spec["revision"]
        cache = models_dir / "hub"
        try:
            self._model = SentenceTransformer(
                repo,
                revision=self.revision,
                cache_folder=str(cache),
                device="cpu",
                local_files_only=True,
            )
        except OSError as exc:
            raise EmbeddingError(f"{repo}@{self.revision[:8]} missing from {cache}: {exc}") from exc
        self.dim = int(self._model.get_embedding_dimension())
        self.id = f"local/{repo.split('/')[-1]}/{self.revision[:12]}/{self.dim}/{PP_HASH}"

    async def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        vecs = await asyncio.to_thread(
            self._model.encode,
            texts,
            batch_size=64,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return l2_normalize(vecs)


class OpenAIEmbedder:
    outlier_cos = 0.30
    max_batch = 2048

    def __init__(
        self, api_key: str, model: str = "text-embedding-3-small", dim: int = 1536
    ) -> None:
        from openai import AsyncOpenAI

        # Retries are handled by EmbeddingIndex so that every backend gets the same policy.
        self._client = AsyncOpenAI(api_key=api_key, max_retries=0, timeout=30)
        self.model = model
        self.dim = dim
        self.id = f"openai/{model}/api/{dim}/{PP_HASH}"

    async def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        out: list[np.ndarray] = []
        for i in range(0, len(texts), self.max_batch):
            batch = texts[i : i + self.max_batch]
            try:
                resp = await self._client.embeddings.create(model=self.model, input=batch)
            except Exception as exc:  # openai raises several unrelated error types
                raise EmbeddingError(f"openai embeddings failed: {type(exc).__name__}") from exc
            rows = sorted(resp.data, key=lambda d: d.index)
            out.append(np.array([r.embedding for r in rows], dtype=np.float32))
        vecs = np.vstack(out)
        if vecs.shape[1] != self.dim:
            raise EmbeddingError(f"expected dim {self.dim}, got {vecs.shape[1]}")
        return l2_normalize(vecs)


def local_available(models_dir: Path) -> bool:
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        return False
    spec = read_models_lock(models_dir).get("models", {}).get(MINILM_REPO)
    if not spec:
        return False
    snap = models_dir / "hub" / f"models--{MINILM_REPO.replace('/', '--')}" / "snapshots"
    return (snap / spec["revision"]).exists()


def build_embedders(
    choice: str,
    openai_key: str | None,
    models_dir: Path,
    openai_model: str = "text-embedding-3-small",
) -> list[Embedder]:
    """Primary backend first, then the fallbacks a new index generation may switch to."""
    local: list[Embedder] = []
    if choice in ("local", "auto", "openai") and local_available(models_dir):
        try:
            local = [LocalEmbedder(models_dir)]
        except EmbeddingError as exc:
            log.warning("local embedder unavailable: %s", exc)
    if choice == "local" and not local:
        raise EmbeddingError(
            "local embedder requested but the model is not set up; run `make models`"
        )
    if choice == "hashing":
        return [HashingEmbedder()]
    if choice == "openai" or (choice == "auto" and openai_key):
        if not openai_key:
            raise EmbeddingError("OPENAI_API_KEY is not set")
        return [OpenAIEmbedder(openai_key, openai_model), *local, HashingEmbedder()]
    return [*local, HashingEmbedder()]

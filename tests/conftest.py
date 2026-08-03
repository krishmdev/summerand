from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

for key in ("OPENAI_API_KEY", "POLYGON_API_KEY", "CRYPTOPANIC_TOKEN"):
    os.environ.pop(key, None)
os.environ.setdefault("HF_HUB_OFFLINE", "1")


@pytest.fixture
def settings(tmp_path):
    from summerand.config import Settings

    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.sqlite'}",
        summerand_embedder="hashing",
        summerand_llm="off",
    )


@pytest.fixture
def watchlist():
    from summerand.config import load_watchlist

    return load_watchlist(ROOT / "config")

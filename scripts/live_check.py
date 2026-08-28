"""Small live check with real keys: OpenAI embeddings (text-embedding-3-small) and LLM briefs.

    OPENAI_API_KEY=... uv run python scripts/live_check.py --fixture fixtures/tiny_feed.jsonl --llm
    OPENAI_API_KEY=... uv run python scripts/live_check.py --fixture fixtures/demo_feed.jsonl.gz

Writes results/live_check_<fixture>.json with what was used and what happened. Costs cents.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from summerand.config import REPO_ROOT, Settings
from summerand.stack import build_stack, run_replay


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", type=Path, required=True)
    ap.add_argument("--llm", action="store_true", help="also write briefs with the chat model")
    args = ap.parse_args()
    settings = Settings(
        database_url="sqlite:///:memory:",
        summerand_embedder="openai",
        summerand_llm="openai" if args.llm else "off",
    )
    stack = build_stack(settings)
    t0 = time.monotonic()
    await run_replay(stack, args.fixture, None)
    p = stack.pipeline
    briefs = stack.store.briefs()
    methods: dict[str, int] = {}
    for b in briefs:
        methods[b["method"]] = methods.get(b["method"], 0) + 1
    rejected = [b for b in briefs if b["problems"]]
    snap = stack.store.latest_snapshot()
    result = {
        "date": time.strftime("%Y-%m-%d"),
        "fixture": args.fixture.name,
        "embedder_id": p.index.gen.embedder_id,
        "generation": p.index.generation,
        "embedded_in_window": len(p.index.gen.vectors),
        "brief_model": settings.summerand_brief_model if args.llm else None,
        "seconds": round(time.monotonic() - t0, 1),
        "status": p.status(stack.driver),
        "silhouette_last_selection": {k: round(v, 4) for k, v in p.clusterer.silhouette.items()},
        "briefs_by_method": methods,
        "llm_calls": p.briefer.llm_calls,
        "llm_rejected": p.briefer.llm_rejected,
        "rejection_reasons": sorted({r for b in rejected for r in b["problems"]})[:20],
        "top_stories": [
            {"rank": s["rank"], "label": s["label"], "size": s["size"], "score": s["score"]}
            for s in (snap or {"stories": []})["stories"][:5]
        ],
        "sample_llm_briefs": [b["text"] for b in briefs if b["method"] == "llm"][:3],
    }
    out = REPO_ROOT / "results" / f"live_check_{args.fixture.name.split('.')[0]}.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())

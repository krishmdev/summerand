"""Does the impact ranking put stories that precede big price moves near the top?

Replays the demo fixture, then for every ranking snapshot t (every 10 minutes of event time):
  candidates  stories with at least one ticker that has prices at t and t+60m
  positive    some ticker moves |ln P(t+60m)/P(t)| > 2 * sigma_1m * sqrt(60), where sigma_1m is
              realized 1-minute volatility over [t-120m, t]. Only the future window is used for
              the label; the ranking at t only ever saw data up to t.
Rankers: the pipeline's impact order, recency (latest article first), size (most articles first)
and random (mean of 200 shuffles). Metrics are averaged over snapshots with at least one positive.

    uv run python scripts/ranking_eval.py [--embedder local] [--out results/ranking_eval.json]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import subprocess
from pathlib import Path

from summerand.config import REPO_ROOT, Settings
from summerand.ingest.replay import read_fixture
from summerand.market.bars import PriceBook
from summerand.schemas import MINUTE_MS, Bar
from summerand.stack import build_stack, run_replay

HORIZON = 60 * MINUTE_MS
STRIDE = 10 * MINUTE_MS


def price_book(fixture: Path) -> PriceBook:
    pb = PriceBook(max_age_ms=10 * MINUTE_MS)
    for r in read_fixture(fixture):
        if r["type"] == "bar":
            pb.add(
                Bar(
                    symbol=r["symbol"],
                    ts_ms=r["start_ms"],
                    open=r["o"],
                    high=r["h"],
                    low=r["l"],
                    close=r["c"],
                    volume=r["v"],
                    venue=r.get("venue", ""),
                )
            )
    return pb


def label(story: dict, t: int, pb: PriceBook) -> bool | None:
    seen = False
    for sym in story["tickers"]:
        r = pb.log_return(sym, t, t + HORIZON)
        sigma = pb.realized_vol(sym, t - 120 * MINUTE_MS, t)
        if r is None or sigma is None:
            continue
        seen = True
        if abs(r) > 2 * sigma * math.sqrt(60):
            return True
    return False if seen else None


def precision_at(labels: list[bool], k: int) -> float:
    return sum(labels[:k]) / min(k, len(labels))


def ndcg_at(labels: list[bool], k: int) -> float:
    dcg = sum(1 / math.log2(i + 2) for i, y in enumerate(labels[:k]) if y)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(k, sum(labels))))
    return dcg / ideal if ideal else 0.0


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", type=Path, default=REPO_ROOT / "fixtures" / "demo_feed.jsonl.gz")
    ap.add_argument("--embedder", default="local")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "results" / "ranking_eval.json")
    args = ap.parse_args()

    settings = Settings(
        _env_file=None,
        database_url="sqlite:///:memory:",
        summerand_embedder=args.embedder,
        summerand_llm="off",
    )
    stack = build_stack(settings)
    await run_replay(stack, args.fixture, None)
    snaps = stack.store.snapshots()
    pb = price_book(args.fixture)

    rankers = {
        "impact": lambda c: c,
        "recency": lambda c: sorted(c, key=lambda s: (-s["last_ms"], s["cluster_id"])),
        "size": lambda c: sorted(c, key=lambda s: (-s["size"], s["cluster_id"])),
    }
    scores = {name: {"p@5": [], "ndcg@10": []} for name in [*rankers, "random"]}
    rng = random.Random(0)
    evaluated = with_candidates = n_candidates = n_positive = 0
    positive_snapshot_sizes = []
    p5_rankable = ndcg_rankable = 0
    last_t = None
    for snap in snaps:
        t = snap["ts_ms"]
        if snap["stale"] or (last_t is not None and t - last_t < STRIDE):
            continue
        last_t = t
        cands = []
        for s in snap["stories"]:
            y = label(s, t, pb)
            if y is not None:
                cands.append((s, y))
        if not cands:
            continue
        with_candidates += 1
        n_candidates += len(cands)
        n_positive += sum(y for _, y in cands)
        if not any(y for _, y in cands):
            continue
        evaluated += 1
        positive_snapshot_sizes.append(len(cands))
        # When all candidates fit in the cutoff, P@5 cannot measure ordering.
        p5_rankable += len(cands) > 5 and any(not y for _, y in cands)
        ndcg_rankable += len(cands) > 1 and any(not y for _, y in cands)
        ys = {s["cluster_id"]: y for s, y in cands}
        stories = [s for s, _ in cands]
        for name, order in rankers.items():
            labels = [ys[s["cluster_id"]] for s in order(stories)]
            scores[name]["p@5"].append(precision_at(labels, 5))
            scores[name]["ndcg@10"].append(ndcg_at(labels, 10))
        p, n = [], []
        for _ in range(200):
            shuffled = [ys[s["cluster_id"]] for s in rng.sample(stories, len(stories))]
            p.append(precision_at(shuffled, 5))
            n.append(ndcg_at(shuffled, 10))
        scores["random"]["p@5"].append(sum(p) / len(p))
        scores["random"]["ndcg@10"].append(sum(n) / len(n))

    mean = lambda xs: round(sum(xs) / len(xs), 4) if xs else None  # noqa: E731
    result = {
        "fixture": args.fixture.name,
        "fixture_sha256": hashlib.sha256(args.fixture.read_bytes()).hexdigest(),
        "evaluation_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "embedder": stack.pipeline.index.gen.embedder_id,
        "horizon_min": HORIZON // MINUTE_MS,
        "stride_min": STRIDE // MINUTE_MS,
        "snapshots_with_priced_candidates": with_candidates,
        "snapshots_evaluated": evaluated,
        "candidates": n_candidates,
        "positive_candidates": n_positive,
        "positive_rate": round(n_positive / n_candidates, 4) if n_candidates else None,
        "positive_snapshot_candidate_counts": positive_snapshot_sizes,
        "snapshots_where_p5_can_vary_by_order": p5_rankable,
        "snapshots_where_ndcg10_can_vary_by_order": ndcg_rankable,
        "metrics": {name: {m: mean(v) for m, v in d.items()} for name, d in scores.items()},
        "git_commit": subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=REPO_ROOT
        ).stdout.strip(),
    }
    manifest_tool = os.environ.get("RUN_MANIFEST", "")
    if manifest_tool:
        out = subprocess.run(
            [
                "python3",
                manifest_tool,
                "script=ranking_eval",
                f"embedder={result['embedder']}",
                "device=cpu",
            ],
            capture_output=True,
            text=True,
        )
        result["run_manifest"] = json.loads(out.stdout)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

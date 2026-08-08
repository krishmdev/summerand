"""Download pinned model snapshots into .models/hub and check them against models.lock.

    python scripts/fetch_models.py              # download (if needed) and verify
    python scripts/fetch_models.py --write-lock --revision REPO=SHA   # pin a new revision

The runtime never downloads anything: LocalEmbedder loads from .models/hub with
local_files_only=True at the revision recorded here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from huggingface_hub import snapshot_download

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "models.lock"
CACHE = ROOT / ".models" / "hub"

# Only what sentence-transformers needs for a torch CPU load; skips onnx/openvino/tf copies.
ALLOW = {
    "sentence-transformers/all-MiniLM-L6-v2": [
        "modules.json",
        "config.json",
        "config_sentence_transformers.json",
        "sentence_bert_config.json",
        "model.safetensors",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "vocab.txt",
        "1_Pooling/config.json",
    ]
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(repo: str, revision: str) -> dict[str, str]:
    snap = Path(
        snapshot_download(repo, revision=revision, cache_dir=CACHE, allow_patterns=ALLOW[repo])
    )
    return {rel: sha256(snap / rel) for rel in sorted(ALLOW[repo])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-lock", action="store_true")
    ap.add_argument("--revision", action="append", default=[], help="REPO=SHA to pin")
    args = ap.parse_args()

    lock = json.loads(LOCK.read_text()) if LOCK.exists() else {"models": {}}
    for pin in args.revision:
        repo, rev = pin.split("=", 1)
        lock["models"].setdefault(repo, {})["revision"] = rev

    ok = True
    for repo, spec in lock["models"].items():
        files = fetch(repo, spec["revision"])
        if args.write_lock:
            spec["files"] = files
            print(f"pinned {repo}@{spec['revision'][:12]} ({len(files)} files)")
            continue
        for rel, digest in spec.get("files", {}).items():
            if files.get(rel) != digest:
                print(f"MISMATCH {repo}/{rel}: {files.get(rel)} != {digest}")
                ok = False
        if ok:
            print(f"ok {repo}@{spec['revision'][:12]} ({len(files)} files verified)")
    if args.write_lock:
        LOCK.write_text(json.dumps(lock, indent=2) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

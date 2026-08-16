"""Egress canary. With SUMMERAND_EGRESS_CHECK=require-blocked, every process type (api, pipeline,
etl, ingest, replay, demo) tries a few external connects at startup and exits if any succeeds.
The offline verification runs everything that way; `summerand egress-check --expect open` is the
companion check that the probe does connect when nothing blocks it."""

from __future__ import annotations

import logging
import socket
import sys

log = logging.getLogger(__name__)

TARGETS = [
    ("1.1.1.1", 443),
    ("api.openai.com", 443),
    ("huggingface.co", 443),
    ("ws-feed.exchange.coinbase.com", 443),
]


def open_targets(timeout: float = 3.0) -> list[str]:
    leaks = []
    for host, port in TARGETS:
        try:
            socket.create_connection((host, port), timeout=timeout).close()
            leaks.append(f"{host}:{port}")
        except OSError:
            pass
    return leaks


def enforce(mode: str, process: str) -> None:
    if mode != "require-blocked":
        return
    leaks = open_targets()
    if leaks:
        log.error("egress canary (%s): reachable %s", process, ", ".join(leaks))
        print(f"EGRESS OPEN in {process}: {', '.join(leaks)}", file=sys.stderr, flush=True)
        sys.exit(3)
    print(f"egress canary ({process}): blocked for all {len(TARGETS)} targets", flush=True)

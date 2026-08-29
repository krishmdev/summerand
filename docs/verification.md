# Verification log

What was run on the development machine (Apple M1 Pro, macOS, Docker Desktop), and what happened.
"Offline wrapper" means a sandbox-exec profile that denies all outbound network except localhost
and unsets every provider key; the egress canary (`summerand egress-check`, and the startup check
enabled by `SUMMERAND_EGRESS_CHECK=require-blocked`) confirmed it in each process.

## Offline runs (no keys, no network)

| date | what | result |
|---|---|---|
| 2026-08-29 | `summerand egress-check --expect open`, unsandboxed (companion check) | all 4 targets reachable |
| 2026-08-29 | same, under the offline wrapper | none reachable |
| 2026-08-29 | `make e2e-offline` under the wrapper: canary, `demo --speed 0 --smoke` on the 24h fixture with MiniLM from `.models/`, full pytest | canary blocked; replay 145s, 186 articles, 10,376 bars, 0 late events; smoke passed REST + WS; 95 tests passed |
| 2026-08-29 | `scripts/smoke_extension.py` under the wrapper (Playwright Chromium, unpacked extension) | 7 cards, connection live, brief expands, tickers highlighted on a local page (Bitcoin, Nvidia, NVDA, $ETH, Solana; "AI" and "CEO" not), error state after the server stops |
| 2026-08-29 | `demo --speed 300 --smoke` under the wrapper | 24h fixture replayed in 288s wall time, 0 late events, smoke passed |
| 2026-08-29 | `make compose-offline`: Redpanda, Postgres, replay, ETL, pipeline, API on an `internal: true` network | canary blocked in replay, etl, pipeline, api and smoke; 10,562 records through Redpanda; pipeline finished with 0 late events; smoke from inside the network passed REST + WS |
| 2026-08-30 | `docker run --network none` with the image: canary, then `demo --smoke` on the tiny fixture (the CI offline job) | canary blocked; smoke passed |

The unpaced and 300x replays of the tiny fixture producing identical cluster membership, ranking
snapshots (including every score component) and brief hashes is a test
(`tests/test_replay_e2e.py`).

## Live runs (network, real keys)

| date | what | result |
|---|---|---|
| 2026-08-29 | RSS feeds in `config/sources.yaml` | 15 of 16 respond; Reuters' crypto feed returns 401 (disabled); The Block's old `/feeds/rss` is 404, `/rss.xml` works |
| 2026-08-29 | Coinbase REST candles, Polygon REST minute aggregates (`scripts/record_fixture.py`) | 1,440 bars each for BTC/ETH/SOL; 730-943 bars for each of 7 equities (the free plan includes REST aggregates) |
| 2026-08-30 | Coinbase public WebSocket (`ticker` channel) | ticks for BTC, ETH, SOL parsed with correct UTC timestamps |
| 2026-08-30 | Polygon stocks WebSocket | connects; auth rejected with "Your plan doesn't include websocket access", surfaced as `PolygonAuthError` |
| 2026-08-30 | CryptoPanic `/api/developer/v2/posts/` | 404 "valid paths are /api/{growth,growth_weekly,enterprise}/v2"; `/api/growth/v2` in turn answers "use /api/developer/v2". The v1 path is gone too. Not usable today; the URL stays configurable |
| 2026-08-30 | `summerand live` for about 4 minutes (RSS + Coinbase, MiniLM) | 270 articles, 681 ticks, rankings published; the Fed feed returned 404 twice and was backed off (it answered 200 again later) |
| 2026-08-30 | OpenAI `text-embedding-3-small` and `gpt-4.1-mini` via `scripts/live_check.py` on the tiny fixture | the key had no credits (HTTP 429 `credit_balance_exhausted`), so no live embeddings or LLM briefs. The run did exercise the real failure path: three failed syncs, a switch to embedding generation 2 (MiniLM), 4 stale snapshots served meanwhile, then normal ranking; every brief fell back to extractive (`results/live_check_tiny_feed.json`) |

Not verified here: OpenAI embeddings and LLM briefs against the live API (no credits), Polygon
equities over WebSocket (plan), CryptoPanic (API changes), and GitHub Actions itself (nothing is
pushed from this machine).

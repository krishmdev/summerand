# Summerand

Market news clustering, impact ranking and briefs, streamed to a Chrome side panel.

## Run the recorded demo

`make setup` installs the locked Python dependencies and pinned local embedding model; it needs network access. After setup, replay the included 24-hour fixture without API keys:

```sh
SUMMERAND_EMBEDDER=local SUMMERAND_LLM=off make demo-fast
```

The API serves at `http://127.0.0.1:8000`. To view the side panel, open `chrome://extensions`, enable Developer mode, choose **Load unpacked**, and select this repository's `extension` directory. The demo command replays into a local SQLite database and serves the final state until stopped. `make ext-test` runs the extension's Node tests; `make test-fast` runs the Python tests.

## Ranking evaluation

`make eval` replays the same fixture and compares the pipeline's impact order with recency, cluster size, and 200 seeded random shuffles. A story is labeled positive when a ticker's absolute 60-minute log return exceeds twice its trailing 120-minute realized one-minute volatility scaled by `sqrt(60)`. Only stories with sufficient price history and a future price enter the evaluation. The result is in [`results/ranking_eval.json`](results/ranking_eval.json); the script and fixture hashes in that file identify the evaluated inputs.

This fixture **does not establish a ranking improvement**. Only 7 of 133 snapshots with priced candidates have a positive story, and those 7 contain 1, 1, 1, 1, 4, 2, and 1 candidates. Precision at five therefore cannot vary with order in any evaluated snapshot. NDCG at ten can vary in only two. The positive snapshots are also drawn from one replay and are not independent market events. The scores are descriptive diagnostics, not evidence that impact ranking predicts price moves or beats the baselines. A useful next evaluation needs more independent positive events and multiple candidates per snapshot, with the labeling rule and comparison fixed before inspecting results.

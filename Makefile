# Setup targets need network. Everything else runs offline; for the strict macOS check use
#   scripts/offline-run make e2e-offline  (sandbox-exec: localhost only, keys unset, HF offline)
UV      ?= uv
RUN     := $(UV) run --frozen --offline
RUN_DEMO := $(UV) run --frozen --offline --no-default-groups
export HF_HOME := $(CURDIR)/.models
export HF_HUB_OFFLINE := 1
SPEED   ?= 300
PORT    ?= 8000

.PHONY: setup setup-demo models fixture lint test test-fast ext-test demo demo-fast demo-hashing e2e-offline smoke-extension \
        compose-up compose-demo compose-offline compose-down eval clean

setup:            ## install locked deps (+MiniLM, +Playwright) and pinned models
	$(UV) sync --frozen --extra local --group e2e
	$(UV) run --frozen playwright install chromium
	$(MAKE) models

setup-demo:       ## install only locked core dependencies for the no-key hashing replay
	$(UV) sync --frozen --no-default-groups

models:           ## download all-MiniLM-L6-v2 at the revision in models.lock and verify sha256
	HF_HUB_OFFLINE=0 $(UV) run --frozen python scripts/fetch_models.py

fixture:          ## re-record the 24h demo fixture (network)
	$(UV) run --frozen python scripts/record_fixture.py --hours 24

lint:
	$(RUN) ruff check .
	$(RUN) ruff format --check .

test:
	$(RUN) pytest
	node --test extension/tests/*.test.mjs

test-fast:
	$(RUN) pytest -m "not slow"

ext-test:
	node --test extension/tests/*.test.mjs

demo:             ## replay the fixture at $(SPEED)x and serve on $(PORT)
	$(RUN) summerand demo --speed $(SPEED) --port $(PORT)

demo-fast:
	$(RUN) summerand demo --speed 0 --port $(PORT)

demo-hashing:     ## no-key fixture replay; var/demo-hashing.sqlite is replaced on each run
	$(RUN_DEMO) summerand demo --speed 0 --port $(PORT) --embedder hashing --llm off --db var/demo-hashing.sqlite

e2e-offline:      ## the checks that start servers; run under offline-run
	SUMMERAND_EGRESS_CHECK=require-blocked $(RUN) summerand egress-check --expect blocked
	SUMMERAND_EGRESS_CHECK=require-blocked $(RUN) summerand demo --speed 0 --smoke --port 8765
	$(RUN) pytest
	SUMMERAND_EGRESS_CHECK=require-blocked $(RUN) python scripts/smoke_extension.py

smoke-extension:
	$(RUN) python scripts/smoke_extension.py --shots docs

compose-up:       ## live mode: Redpanda + ingest + etl + pipeline + api + Postgres
	docker compose up -d --build

compose-demo:     ## replay the fixture through Kafka (topics prefixed demo.)
	docker compose --profile demo-kafka up -d --build replay etl-demo pipeline-demo api-demo

compose-offline:  ## the Kafka replay on an internal network, egress canaries on, smoke from inside
	SUMMERAND_REPLAY_SPEED=0 docker compose -f docker-compose.yml -f compose.offline.yml --profile demo-kafka \
	  up --build --abort-on-container-exit --exit-code-from smoke smoke
	docker compose -f docker-compose.yml -f compose.offline.yml --profile demo-kafka down -v

compose-down:
	docker compose --profile demo-kafka --profile cryptopanic down -v

eval:             ## ranking evaluation on the demo fixture
	$(RUN) python scripts/ranking_eval.py

clean:
	rm -rf var .pytest_cache .ruff_cache

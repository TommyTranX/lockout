PY ?= .venv/bin/python
LOCKOUT ?= .venv/bin/lockout
ASSETS = https://raw.githubusercontent.com/datahub-project/static-assets/main/datasets/nyc-taxi

.PHONY: help judge install data quickstart ingest seed arm permit demo counterfactual test clean nuke

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

judge: install data quickstart ingest seed arm demo ## Everything, cold clone to demo

install: ## Create the venv and install Lockout
	uv venv .venv --python 3.11
	uv pip install --python .venv/bin/python -e ".[dev]"

data: data/nyc_taxi_pipeline.db data/nyc_taxi.db ## Fetch DataHub's taxi sample databases

data/nyc_taxi_pipeline.db:
	@mkdir -p data
	curl -fsSL $(ASSETS)/nyc_taxi_pipeline.db -o $@

data/nyc_taxi.db:
	@mkdir -p data
	curl -fsSL $(ASSETS)/nyc_taxi.db -o $@

quickstart: ## Boot DataHub OSS (idempotent). UI: localhost:9002 datahub/datahub
	DATAHUB_TELEMETRY_ENABLED=false $(PY) -m datahub docker quickstart

ingest: ## Ingest the taxi pipeline into DataHub
	DATAHUB_TELEMETRY_ENABLED=false $(PY) -m datahub ingest -c recipes/taxi.yml

seed: ## Emit the ML subgraph, lineage, and structured properties
	$(LOCKOUT) seed

arm: ## Evaluate the rules and write them into DataHub as assertions
	$(LOCKOUT) arm

permit: ## Ask for a training permit
	$(LOCKOUT) permit

demo: ## The full story: blocked run, then the forced run, then the comparison
	@echo "\n=== 1. environment ==="
	-$(LOCKOUT) doctor
	@echo "\n=== 2. the job asks for a permit, and is refused ==="
	-$(LOCKOUT) train --out examples/blocked-run
	@echo "\n=== 3. force it through, to measure what was avoided ==="
	-$(LOCKOUT) train --no-lockout --out examples/forced-run
	@echo "\n=== 4. controlled comparison: clean pipeline vs stale pipeline ==="
	$(MAKE) counterfactual

counterfactual: ## Same model, same holdout, only the data differs
	PYTHONPATH=src $(PY) -c "from lockout.training import counterfactual; print(counterfactual.write('examples'))"

test: ## Run the unit tests (no DataHub required)
	.venv/bin/pytest -q

clean: ## Remove generated artifacts (keeps the downloaded databases)
	rm -rf examples/blocked-run examples/forced-run examples/counterfactual.json

nuke: ## Tear DataHub down completely
	DATAHUB_TELEMETRY_ENABLED=false $(PY) -m datahub docker nuke

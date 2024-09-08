PY ?= .venv/bin/python
SCENARIO ?= bad_deploy

.PHONY: help install test eval simulate serve compose-up compose-down demo demo-down helm-lint

help:
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/ -/'

install: ## create .venv and install with dev deps
	python3 -m venv .venv
	.venv/bin/pip install -q -e '.[dev]'

test: ## run the test suite
	$(PY) -m pytest -q

eval: ## score the agent on the scripted incidents (offline, mock LLM)
	$(PY) -m incidentpilot.cli eval --json eval_results.json

simulate: ## inject SCENARIO and print the agent's report (no services needed)
	$(PY) -m incidentpilot.cli simulate --scenario $(SCENARIO)

serve: ## run the API locally on :8000
	$(PY) -m incidentpilot.cli serve

compose-up: ## NATS + API + generator via docker compose
	docker compose --profile generate up --build

compose-down:
	docker compose --profile generate --profile ollama down -v

helm-lint: ## lint the chart (requires helm)
	helm lint deploy/helm/incidentpilot

demo: ## kind cluster + Helm deploy + live incident report (needs docker, kind, kubectl, helm)
	./scripts/demo.sh

demo-down: ## delete the kind cluster
	kind delete cluster --name $${CLUSTER:-incidentpilot}

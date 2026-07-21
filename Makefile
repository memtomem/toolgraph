# toolgraph dev shortcuts. Run `make` (or `make help`) to list targets.
.DEFAULT_GOAL := help
.PHONY: help install up down clean init crawl ingest demo demo-public smoke-policy-gateway serve check reset test lint fmt

SERVERS ?= examples/servers.yaml
GOVERNANCE ?= examples/governance.yaml
STM_ROOT ?= ../memtomem-stm
ARTIFACTS_DIR ?= /tmp/toolgraph-policy-gateway-evidence

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install deps (incl. dev) via uv
	uv sync --group dev

up: ## Start Neo4j and wait until healthy
	docker compose up -d --wait

down: ## Stop Neo4j (keep data volume)
	docker compose down

clean: ## Stop Neo4j and delete its data volume
	docker compose down -v

check: ## Verify Neo4j connectivity
	uv run toolgraph check

init: ## Create graph constraints
	uv run toolgraph init-schema

crawl: ## Crawl servers from $(SERVERS) into the graph
	uv run toolgraph crawl --servers $(SERVERS)

ingest: ## Ingest authored governance from $(GOVERNANCE)
	uv run toolgraph ingest-manifest --governance $(GOVERNANCE)

reset: ## Delete all graph data (keeps constraints)
	uv run toolgraph reset --yes

serve: ## Run toolgraph as an MCP server (stdio)
	uv run toolgraph serve

demo: ## End-to-end demo (real filesystem server)
	bash scripts/demo.sh

demo-public: ## Cross-server demo (7 real public servers)
	bash scripts/demo-public.sh

smoke-policy-gateway: ## Run Toolgraph -> STM policy-bundle smoke
	uv run --group dev --extra ladybug python scripts/policy_bundle_gateway_smoke.py \
		--memtomem-stm-root "$(STM_ROOT)" --artifacts-dir "$(ARTIFACTS_DIR)"

test: ## Run the test suite (real Neo4j via testcontainers)
	uv run pytest -q

lint: ## Lint with ruff
	uv run ruff check .

fmt: ## Auto-format with ruff
	uv run ruff format .

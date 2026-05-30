#!/usr/bin/env bash
# End-to-end demo / gate evidence. Requires Neo4j running (docker compose up -d).
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== reset graph =="
uv run toolgraph reset --yes

echo; echo "== crawl servers (factual) =="
uv run toolgraph crawl --servers examples/servers.yaml

echo; echo "== ingest authored governance =="
uv run toolgraph ingest-manifest --governance examples/governance.yaml

echo; echo "== Q1: tools public-bot can call that touch a DENY-governed resource =="
uv run toolgraph unsafe-tools public-bot

echo; echo "== Q2: can public-bot call read_file? (verdict + evidence path) =="
uv run toolgraph check-access public-bot read_file

echo; echo "== Q3: blast radius of the secret file =="
uv run toolgraph blast-radius "file:///private/secrets.env"

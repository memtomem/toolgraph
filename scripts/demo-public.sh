#!/usr/bin/env bash
# Gate evidence: 7 real public MCP servers + cross-server governance reasoning.
# A per-server ACL review would clear ci-bot (no filesystem read grant), but
# git_show on another server reaches the same secret — toolgraph catches it.
# Requires Neo4j running (docker compose up -d). First run downloads servers.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== reset =="
uv run toolgraph reset --yes

echo; echo "== crawl 7 real public servers (npm + uvx) =="
uv run toolgraph crawl --servers examples/servers-public.yaml 2>/dev/null

echo; echo "== ingest cross-server governance =="
uv run toolgraph ingest-manifest --governance examples/governance-public.yaml

echo; echo "== ci-bot has NO filesystem read grant — what can it reach? =="
uv run toolgraph unsafe-tools ci-bot

echo; echo "== blast radius of the secret, across both servers =="
uv run toolgraph blast-radius "file:///srv/app/.env"

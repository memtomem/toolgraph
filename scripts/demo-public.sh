#!/usr/bin/env bash
# Gate evidence: 7 real public MCP servers + cross-server governance reasoning.
# A per-server ACL review would clear ci-bot (no filesystem read grant), but
# git_show on another server reaches the same secret — toolgraph catches it.
# Runs in an isolated, disposable Ladybug graph. First run downloads servers.
set -euo pipefail
cd "$(dirname "$0")/.."
STATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/toolgraph-demo-public.XXXXXX")"
trap 'rm -rf "$STATE_DIR"' EXIT
TG=(uv run toolgraph --config "$STATE_DIR/config.json")

echo "== initialize disposable graph =="
uv run toolgraph init --state-dir "$STATE_DIR"

echo; echo "== crawl 7 real public servers (npm + uvx) =="
"${TG[@]}" crawl --servers examples/servers-public.yaml 2>/dev/null

echo; echo "== ingest cross-server governance =="
"${TG[@]}" ingest-manifest --governance examples/governance-public.yaml

echo; echo "== ci-bot has NO filesystem read grant — what can it reach? =="
"${TG[@]}" unsafe-tools ci-bot

echo; echo "== blast radius of the secret, across both servers =="
"${TG[@]}" blast-radius "file:///srv/app/.env"

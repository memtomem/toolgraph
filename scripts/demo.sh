#!/usr/bin/env bash
# End-to-end demo in an isolated, disposable Ladybug graph.
set -euo pipefail
cd "$(dirname "$0")/.."
STATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/toolgraph-demo.XXXXXX")"
trap 'rm -rf "$STATE_DIR"' EXIT
TG=(uv run toolgraph --config "$STATE_DIR/config.json")

echo "== initialize disposable graph =="
uv run toolgraph init --state-dir "$STATE_DIR"

echo; echo "== crawl servers (factual) =="
"${TG[@]}" crawl --servers examples/servers.yaml

echo; echo "== ingest authored governance =="
"${TG[@]}" ingest-manifest --governance examples/governance.yaml

echo; echo "== Q1: tools public-bot can call that touch a DENY-governed resource =="
"${TG[@]}" unsafe-tools public-bot

echo; echo "== Q2: can public-bot call read_file? (verdict + evidence path) =="
"${TG[@]}" check-access public-bot read_file

echo; echo "== Q3: blast radius of the secret file =="
"${TG[@]}" blast-radius "file:///private/secrets.env"

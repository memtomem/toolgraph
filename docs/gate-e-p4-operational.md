# Gate E / P4 operational harness

`scripts/gate_e_operational.py` runs against three explicit Git refs. It clones
those commits locally, creates a disposable target repository and fresh Neo4j
5.26 container, and never resets the source checkouts.

```bash
UV_CACHE_DIR=/private/tmp/gate-e-uv-cache uv run --extra dev python \
  scripts/gate_e_operational.py \
  --toolgraph-root . --toolgraph-ref <toolgraph-sha> \
  --syncmill-root ../syncmill --syncmill-ref <syncmill-sha> \
  --tracegraph-root ../tracegraph --tracegraph-ref <tracegraph-sha> \
  --artifacts-dir /private/tmp/gate-e-evidence
```

The strict matrix covers allow, expected exception, violation, unknown tool,
drift warn/block, unavailable block/warn, and `enabled=false` rollback. Every
block is checked before the fake agent is invoked. The P4 slice then performs
real Tracegraph ingest/query/export, imports the report twice into a temporary
SyncMill board, verifies the shared UUIDv5, records a synthetic `dismissed`
Toolgraph disposition, and confirms preflight, graph generation, blast radius,
source report bytes, and independent board state are unchanged.

Before the P4 slice, Codex, Claude Code, Kimi Code, and Antigravity CLI each run
against the disposable `gate_e` MCP server. The canary requires one observed
`always_ok/succeeded` and one `always_fail/failed` pair per CLI, retries at most
once, and uses the server-side body-free outcome spool as the common oracle.
Provider stdout and stderr stay private and are deleted on success. Missing
authentication, a missing tool call, or an untyped outcome makes Gate E NO-GO;
it is never reported as a skipped pass.

The output is a body-free GO/NO-GO recommendation. It never approves Gate E,
enables strict by default, accepts a synthetic finding as governance, or edits a
manifest. On success only the summary, raw OTLP, normalized artifact, candidate
report, and annotation sidecar remain. On failure the private workspace is
preserved and printed for diagnosis.

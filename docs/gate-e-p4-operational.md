# Gate E / P4 operational harness

`scripts/gate_e_operational.py` runs against three explicit Git refs. It clones
those commits locally, creates a disposable target repository and fresh Neo4j
5.26 container, and never resets the source checkouts.

```bash
UV_CACHE_DIR=/private/tmp/gate-e-uv-cache uv run --group dev python \
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

Before the P4 slice, the three SyncMill AgentRunner providers — Codex, Claude
Code, and Kimi Code — each run against the disposable `gate_e` MCP server. The
canary requires exactly one ordered `always_ok/succeeded` and
`always_fail/failed` pair per CLI, retries at most once, and uses the server-side
body-free outcome spool as the common oracle.
Provider stdout and stderr stay private and are deleted on success. Missing
authentication, a missing tool call, or an untyped outcome makes Gate E NO-GO;
it is never reported as a skipped pass.

Antigravity CLI is an experimental compatibility surface, not a SyncMill
AgentRunner, and is explicitly excluded from the Gate E verdict. The version-2
summary records that policy in `provider_scope`; Antigravity is never presented
as passed, skipped, or unavailable, and the operational harness does not invoke
`agy` or read its user configuration.

The output is a body-free GO/NO-GO recommendation. It never approves Gate E,
enables strict by default, accepts a synthetic finding as governance, or edits a
manifest. On success only the summary, raw OTLP, normalized artifact, candidate
report, and annotation sidecar remain. On failure the private workspace is
preserved and printed for diagnosis.

## Fixture-only validation at explicit commits

Use the following for integration acceptance without paid agent calls. The
arguments must be full 40-character commit SHAs already present in the source
repositories. No source ref is fetched or moved, and dirty source files are not
copied. Each package and governance fixture comes from its requested commit.
The runner's own digest is recorded separately from the tested package SHAs.

```bash
uv run --group dev python scripts/ecosystem_smoke.py \
  --toolgraph-ref <toolgraph-sha> \
  --syncmill-root ../syncmill --syncmill-ref <syncmill-sha> \
  --tracegraph-root ../tracegraph --tracegraph-ref <tracegraph-sha> \
  --artifacts-dir /private/tmp/ecosystem-evidence
```

For Toolgraph-to-STM bundle validation, prepare clean checkouts at the desired
SHAs and sync each checkout from its frozen lockfile, including Toolgraph's dev
group and Ladybug extra. Run from that Toolgraph checkout:

```bash
uv run --group dev --extra ladybug python scripts/policy_bundle_gateway_smoke.py \
  --toolgraph-ref <toolgraph-sha> \
  --memtomem-stm-root ../memtomem-stm --memtomem-stm-ref <stm-sha> \
  --artifacts-dir /private/tmp/gateway-evidence
```

The gateway runner rejects dirty checkouts and mismatched HEADs before starting
MCP processes, and checks again before reporting success. Both summaries record
installed dependencies and lockfile digests. Use a fresh artifacts directory per
run. These tests are separate from the paid-provider Gate E canaries above;
they do not promote a persistent project's configuration.

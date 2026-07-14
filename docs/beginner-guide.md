# toolgraph Beginner Guide

This guide takes a first-time user from an empty checkout to one verified
policy bundle. The default path uses the embedded Ladybug graph and a bundled
MCP fixture, so it needs neither Docker nor Node.js.

Korean version: [`docs/ko-beginner-guide.md`](ko-beginner-guide.md)

Toolgraph is an advisory analyzer and policy compiler, not a runtime proxy. It
crawls MCP tool contracts, combines them with operator-authored grants and data
flows, and explains `Agent -> Tool -> Resource -> Policy` paths. A gateway such
as memtomem-stm consumes the resulting JSON bundle and performs the actual
runtime enforcement.

## Prerequisites

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)

## 1. Initialize a local graph

```bash
uv sync --extra dev --extra ladybug
uv run toolgraph init
```

`init` creates `.toolgraph/config.json` and a local Ladybug database. Toolgraph
commands own that database sequentially; the gateway never opens it.

## 2. Crawl the offline MCP fixture

```bash
uv run toolgraph crawl --servers examples/servers-policy-gateway.yaml
```

The fixture exposes two tools:

- `policy-gateway::read_note` — a read-only, idempotent tool.
- `policy-gateway::publish_note` — a mutating tool that writes to a governed
  draft resource.

## 3. Load the authored policy

```bash
uv run toolgraph ingest-manifest \
  --governance examples/governance-policy-gateway.yaml \
  --strict-drift
```

The example grants `vibe-coder` both tools but places the draft resource under
a DENY policy. Every authored edge includes a local evidence pointer.

## 4. Inspect the decision

```bash
uv run toolgraph eligible-tools vibe-coder \
  policy-gateway::read_note policy-gateway::publish_note \
  --profile review
uv run toolgraph selection-explain vibe-coder policy-gateway::publish_note
```

`read_note` is eligible. `publish_note` is rejected with an explainable policy
path. Toolgraph is reporting the decision here; it has not intercepted a call.

## 5. Compile the gateway bundle

```bash
uv run toolgraph policy compile \
  --agent vibe-coder \
  --profile review \
  --output .toolgraph/policy-bundle.json
```

Success prints JSON containing the output path, exact byte digest, graph
instance/generation, and eligible/rejected counts. The artifact is canonical
UTF-8 JSON, written privately with atomic replacement.

Start with `review`: the reference gateway keeps rejected tools visible and
records would-block calls. After inspecting the decisions, compile a matching
`strict` bundle to hide and block rejected tools.

## 6. Enforce it with memtomem-stm

Toolgraph and the gateway remain separate packages. Follow the
[memtomem-stm Toolgraph policy gateway guide](https://github.com/memtomem/memtomem-stm/blob/main/docs/guides/toolgraph-policy-gateway.md)
to point STM at this bundle, run `mms gateway status` and `explain`, and connect
it to Codex or Claude Code.

The server name used by STM should match the name crawled by Toolgraph
(`policy-gateway` here). If they differ, configure STM's `server_name_map`.

## Useful audits and experiments

```bash
uv run toolgraph unsafe-tools vibe-coder
uv run toolgraph unmapped-tools
uv run toolgraph unbacked-edges
uv run toolgraph drift
uv run toolgraph blast-radius draft-publish-deny
```

Good first experiments are removing the `read_note` grant, changing the draft
policy binding, and recompiling. Always rerun `ingest-manifest` before
compilation. Invalid manifests are rejected without replacing the previous
graph state; failed compilation leaves the previous bundle intact.

## Shared or fleet operation

Ladybug is the default single-process local backend. Use Neo4j when multiple
processes or operators need a shared graph:

```bash
docker compose up -d --wait
cp .env.example .env
uv run toolgraph init --backend neo4j
```

The larger `scripts/demo.sh` and `scripts/demo-public.sh` examples use real MCP
servers and may require Docker, Node.js, `npx`, and network access.

## Troubleshooting

- `AGENT_NOT_FOUND`: the same agent must exist in `agents` and the relevant
  grants.
- `TOOL_NOT_FOUND`: rerun crawl and use the qualified
  `server::tool` identifier.
- `DRIFTED`: governance references a tool the latest crawl no longer exposes.
- Bundle compilation refuses missing governance state or graph identity rather
  than publishing a misleading artifact.
- A `DENY` result is enforced only after a gateway consumes the bundle.

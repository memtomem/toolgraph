# toolgraph Beginner Guide

This guide takes a first-time user from installation to one verified policy
bundle. The package includes the Ladybug backend and MCP fixture, so the
default path needs no repository clone, Docker, or Node.js.

Korean version: [`docs/ko-beginner-guide.md`](ko-beginner-guide.md)

Toolgraph is an advisory analyzer and policy compiler, not a runtime proxy. It
crawls MCP tool contracts, combines them with operator-authored grants and data
flows, and explains `Agent -> Tool -> Resource -> Policy` paths. A gateway such
as memtomem-stm consumes the resulting JSON bundle and performs the actual
runtime enforcement.

## Prerequisites

- Python 3.12, 3.13, or 3.14
- [`uv`](https://docs.astral.sh/uv/)

## 1. Install and create the quickstart

> **Not on PyPI yet.** Install from source until `v0.0.1` is published.

```bash
git clone https://github.com/memtomem/toolgraph.git
cd toolgraph
uv tool install --from '.[ladybug]' toolgraph
cd ..
toolgraph example init toolgraph-quickstart
cd toolgraph-quickstart
toolgraph init
```

`init` creates `.toolgraph/config.json` and a local Ladybug database. Toolgraph
commands own that database sequentially; the gateway never opens it.

## 2. Crawl the offline MCP fixture

```bash
toolgraph crawl --servers servers.yaml
```

The fixture exposes two tools:

- `policy-gateway::read_note` — a read-only, idempotent tool.
- `policy-gateway::publish_note` — a mutating tool that writes to a governed
  draft resource.

## 3. Load the authored policy

```bash
toolgraph ingest-manifest \
  --governance governance.yaml \
  --strict-drift
```

The example grants `vibe-coder` both tools but places the draft resource under
a DENY policy. Every authored edge includes a local evidence pointer.

## 4. Inspect the decision

```bash
toolgraph eligible-tools vibe-coder \
  policy-gateway::read_note policy-gateway::publish_note \
  --profile review
toolgraph selection-explain vibe-coder policy-gateway::publish_note
```

`read_note` is eligible. `publish_note` is rejected with an explainable policy
path. Toolgraph is reporting the decision here; it has not intercepted a call.

## 5. Compile the gateway bundle

```bash
toolgraph policy compile \
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
toolgraph unsafe-tools vibe-coder
toolgraph unmapped-tools
toolgraph unbacked-edges
toolgraph drift
toolgraph blast-radius draft-publish-deny
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
toolgraph init --backend neo4j
```

The compose file is development-only: it has a known password and loopback-only
ports, not production authentication. The repository's larger demo scripts use
disposable Ladybug databases; the public-server demo may require Node.js,
`npx`, and network access.

In fleet configurations, give every server an explicit unique `name`. Unnamed
stdio diagnostics intentionally show only the executable (for example,
`stdio:npx`) so command arguments and credentials never enter logs.

## Troubleshooting

- `AGENT_NOT_FOUND`: the same agent must exist in `agents` and the relevant
  grants.
- `TOOL_NOT_FOUND`: rerun crawl and use the qualified
  `server::tool` identifier.
- `DRIFTED`: governance references a tool the latest crawl no longer exposes.
- Bundle compilation refuses missing governance state or graph identity rather
  than publishing a misleading artifact.
- A `DENY` result is enforced only after a gateway consumes the bundle.

# toolgraph Beginner Guide

This guide takes a first-time user from installation to one verified policy
bundle. The package includes the Ladybug backend and MCP fixture, so the
default path needs no repository clone, Docker, or Node.js.

Korean version: [`docs/ko-beginner-guide.md`](ko-beginner-guide.md)

To run the same path one step at a time with the outputs in front of you, open
the notebook
[`docs/notebooks/01-first-policy-bundle.ipynb`](notebooks/01-first-policy-bundle.ipynb).

Toolgraph is an advisory analyzer and policy compiler, not a runtime proxy. It
crawls MCP tool contracts, combines them with operator-authored grants and data
flows, and explains `Agent -> Tool -> Resource -> Policy` paths. A gateway such
as memtomem-stm consumes the resulting JSON bundle and performs the actual
runtime enforcement.

## Prerequisites

- Python 3.12, 3.13, or 3.14
- [`uv`](https://docs.astral.sh/uv/)

## 1. Install and create the quickstart

```bash
uv tool install "toolgraph[ladybug]"
toolgraph example init toolgraph-quickstart
cd toolgraph-quickstart
toolgraph init
```

`init` creates `.toolgraph/config.json` and a local Ladybug database. Toolgraph
commands own that database sequentially; the gateway never opens it.

Check the setup before going further:

```bash
toolgraph doctor
```

`doctor` checks four things: the config file, the database connection, the
schema, and the graph state. Right after `init` all four report `PASS` and the
last line is `Overall status: HEALTHY`. The `Configuration` entry also shows
`backend_source`, which says where the backend choice came from:
`config-file` here. Any `FAIL` makes `doctor` exit 1; see
[Troubleshooting](#troubleshooting). `toolgraph doctor --json` prints the same
report as JSON.

## 2. Crawl the offline MCP fixture

```bash
toolgraph crawl --servers servers.yaml
```

`servers.yaml` declares your whole fleet. Its `servers` key is required:
`servers: []` means "no servers" and, by default, retires every crawled
server (`--no-prune` keeps them); authored governance is never deleted by
a crawl. A blank file or one without the key is rejected before any server
is contacted.

The fixture exposes two tools:

- `policy-gateway::read_note` — a read-only, idempotent tool.
- `policy-gateway::publish_note` — a mutating tool that writes to a governed
  draft resource.

## 3. Load the authored policy

Preview first. `--dry-run` checks the manifest against the graph and prints
what would change, without writing anything:

```bash
toolgraph ingest-manifest \
  --governance governance.yaml \
  --strict-drift \
  --dry-run
```

It prints `DRY-RUN: manifest at governance.yaml would apply cleanly` and a
delta summary (one agent, one policy, two grants, two data-access edges, one
policy binding to be added). A manifest that would be rejected prints
`DRY-RUN REJECTED` with the reason and exits 1; a misspelled tool name is the
usual cause. Then load it for real:

```bash
toolgraph ingest-manifest \
  --governance governance.yaml \
  --strict-drift
```

The example grants `vibe-coder` both tools but places the draft resource under
a DENY policy. Every authored edge carries `provenance` with an evidence
pointer, so `toolgraph unbacked-edges` returns an empty list. Delete the
`provenance` block under one of the `data_access` entries and rerun the
command with `--dry-run`: the delta shows `Provenance modified: 1`. Rerun it
without `--dry-run`, and `unbacked-edges` names that edge. Put the block back
and rerun the command before continuing; the rest of this guide assumes the
original manifest. Grants are audited separately, under
`unbacked-edges --include-grants`.

An empty result means every edge has a pointer, not that the claims behind
them are true. The demo tools return strings and touch no file, so the
READS/WRITES entries are synthetic; their evidence says so. Verifying a
pointer is the reviewer's work, and requiring one is what makes that work
possible.

## 4. Inspect the decision

```bash
toolgraph eligible-tools vibe-coder \
  policy-gateway::read_note policy-gateway::publish_note \
  --profile review
toolgraph selection-explain vibe-coder policy-gateway::publish_note \
  --profile review
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

Success prints JSON describing the bundle -- output path, exact byte digest,
graph instance/generation, and eligible/rejected counts -- not the bundle
itself. The artifact is canonical
UTF-8 JSON, written privately with atomic replacement.

Start with `review`: the reference gateway keeps rejected tools visible and
records would-block calls. After inspecting the decisions, compile a matching
`strict` bundle to hide and block rejected tools.

That is the end of the standalone quickstart. Everything below is optional.

## 6. Optional: enforce it with memtomem-stm

Toolgraph never blocks a call itself, so a gateway is how a DENY becomes real.
The reference consumer is a separate package and is not required to use
Toolgraph. Follow the
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

Two of these return a row on purpose. `unsafe-tools` reports `publish_note`
reaching `draft-publish-deny`, classified as a violation, and `blast-radius`
reports the path that policy covers. Naming that reach is what they are for, so
on this fixture a non-empty result is the correct one. The other three come
back empty.

Good first experiments are removing the `read_note` grant, changing the draft
policy binding, and recompiling. Always rerun `ingest-manifest` before
compilation. Invalid manifests are rejected without replacing the previous
graph state; failed compilation leaves the previous bundle intact.

## Shared or fleet operation

Ladybug is the default single-process local backend. Use Neo4j 5.7 or later
when multiple processes or operators need a shared graph. This part needs the repository:
`docker-compose.yml` and `.env.example` ship with the clone, not with the
package or the generated quickstart. The Neo4j driver is the `neo4j` extra,
so the first command reinstalls Toolgraph with it.

```bash
uv tool install "toolgraph[neo4j]"
git clone https://github.com/memtomem/toolgraph
cd toolgraph
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

Start with `toolgraph doctor` and read the `backend_source` line under
`Configuration`.

- The `Configuration` entry FAILs with `no backend configured` and shows
  `backend_source: none`: neither a config file nor the environment chose a
  backend, and Toolgraph does not guess one. When it also shows
  `exists: False`, you are outside the quickstart directory or `toolgraph init`
  was skipped. `cd` into the directory or run `init`. Every command that opens
  the graph stops with the same message.
- The `Configuration` entry FAILs with
  `conflicting backend configuration`: `TOOLGRAPH_BACKEND` or
  `TOOLGRAPH_DB_PATH`, set in your shell or in a `.env` file in this
  directory, disagrees with `.toolgraph/config.json`. The message names the
  variable. (`backend_source` reads `env` only when `TOOLGRAPH_BACKEND` is
  set.) Unset it in the shell, or remove it from `.env`, since the next
  command loads `.env` again. Alternatively, pass
  `--config .toolgraph/config.json` before the command
  (`toolgraph --config .toolgraph/config.json doctor`). Until then, commands
  such as `crawl` and `eligible-tools` stop with the same message.
- `Schema Integrity` FAIL: run `toolgraph init-schema`, which creates missing
  tables or constraints and stamps a database that has no schema version. If
  it refuses with `backend schema version N does not match this build's M`, a
  different Toolgraph version created the database; it is left unchanged, so
  use that version or a fresh database.
- `DRY-RUN REJECTED` or `REJECTED`: the warnings name the problem, and the
  graph is unchanged.
- `AGENT_NOT_FOUND`: the same agent must exist in `agents` and the relevant
  grants.
- `TOOL_NOT_FOUND`: rerun crawl and use the qualified
  `server::tool` identifier.
- `DRIFTED`: governance references a tool the latest crawl no longer exposes.
- Bundle compilation refuses missing governance state or graph identity rather
  than publishing a misleading artifact.
- A `DENY` result is enforced only after a gateway consumes the bundle.

# toolgraph

A graph-native registry for MCP tools. It crawls MCP servers into a Neo4j graph,
lets you author governance (who can call what, what touches which resource, which
resources are under which policy), and answers **explainable reachability
questions** that a vector/embedding tool registry structurally cannot:

- *Which tools can this agent call that touch a resource under a DENY policy?*
- *Is this agent allowed to call this tool — and does the call cross a deny policy?*
- *If this resource (or policy) changes, which agents and tools are affected?*

Each answer comes back with the **exact graph path as evidence**, not a bare boolean.

> **Advisory, not enforcement.** toolgraph analyzes and explains; it does **not**
> sit in the traffic path and does not block calls at runtime. Think of it as a
> risk/impact analyzer for your MCP tool estate. (For runtime enforcement you'd
> pair it with a gateway.)

## Why a graph

Discovery (“find a tool like X”) is well served by vector search. But
dependencies, ACLs, and policy are **many-to-many relationships** —
`Agent → Tool → Resource → Policy` across four node types. That is a graph
reachability problem, not a similarity problem. Every edge here is either a
crawled fact or an operator-authored assertion; nothing is LLM-guessed.

## Graph model

```
(MCPServer)-[:EXPOSES]->(Tool)            # crawled (factual)
(MCPServer)-[:PROVIDES]->(Resource)       # crawled (factual)
(Agent)-[:CAN_CALL]->(Tool)               # authored
(Tool)-[:READS|WRITES]->(Resource)        # authored
(Resource|Tool)-[:GOVERNED_BY]->(Policy)  # authored
```

`Policy{id, effect: ALLOW|DENY, scope, description}`.

## Quickstart

```bash
# 1. start Neo4j (pinned 5.26 community)
docker compose up -d

# 2. install
uv sync --extra dev
cp .env.example .env            # defaults match docker-compose

# 3. run the end-to-end demo (crawls the real filesystem MCP server)
bash scripts/demo.sh

# or the cross-server demo: 7 real public servers (npm + uvx), and a secret
# reachable via tools on two different servers (filesystem AND git)
bash scripts/demo-public.sh
```

The cross-server demo is the headline: `ci-bot` has no filesystem read grant,
yet `unsafe-tools ci-bot` flags it — because `git_show` on a *different* server
reaches the same governed secret. A per-server ACL review misses this; the graph
catches it.

Or step by step:

```bash
uv run toolgraph check                                   # verify Neo4j connectivity
uv run toolgraph init-schema                             # uniqueness constraints
uv run toolgraph crawl --servers examples/servers.yaml   # crawl -> load factual graph
uv run toolgraph ingest-manifest --governance examples/governance.yaml

uv run toolgraph unsafe-tools public-bot
uv run toolgraph check-access public-bot read_file
uv run toolgraph blast-radius "file:///private/secrets.env"
```

## toolgraph as an MCP server

toolgraph exposes its three queries as MCP tools, so an agent can ask the
registry about itself over the same protocol toolgraph crawls:

```bash
uv run toolgraph serve            # stdio
uv run toolgraph serve --http     # streamable-http
```

Tools: `check_access`, `unsafe_callable_tools`, `blast_radius`.

## Inputs

- `servers.yaml` — MCP servers to crawl (stdio `command`/`args`, or http `url`).
- `governance.yaml` — authored agents, policies, `CAN_CALL` grants, `READS/WRITES`
  data access, and `GOVERNED_BY` mappings. See `examples/`.

## Layout

```
toolgraph/
  crawler/   connect to MCP servers (stdio + streamable-http), enumerate tools/resources
  graph/     Neo4j driver, schema/constraints, idempotent loader, the 3 governance queries
  manifest/  parse + ingest authored governance (idempotent)
  server/    FastMCP server exposing the queries as MCP tools
  cli.py     crawl / ingest-manifest / check-access / unsafe-tools / blast-radius / serve / reset
```

## Tests

```bash
uv run pytest          # uses a real Neo4j via testcontainers (no Cypher mocking)
```

## Scope (MVP)

This is a deliberately thin, thesis-validating MVP. **In:** factual crawl +
authored governance + the three explainable queries. **Deferred** until the
thesis proves out: LLM-inferred dependency/version edges, vector search, an
official-registry importer, and runtime enforcement.

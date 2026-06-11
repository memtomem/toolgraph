# toolgraph

A graph-native registry for MCP tools. It crawls MCP servers into a Neo4j graph,
lets you author governance (who can call what, what touches which resource, which
resources are under which policy), and answers **explainable reachability
questions** over the result:

- *Which tools can this agent call that touch a resource under a DENY policy?*
- *Is this agent allowed to call this tool — and does the call cross a deny policy?*
- *If this resource (or policy) changes, which agents and tools are affected?*

Each answer comes back with the **exact graph path as evidence**, not a bare boolean,
and carries per-edge **provenance** (crawled fact vs operator assertion vs inferred,
with a free-text evidence pointer) so reviewers can verify the claim.

> **Advisory, not enforcement.** toolgraph analyzes and explains; it does **not**
> sit in the traffic path and does not block calls at runtime. Think of it as a
> risk/impact analyzer for your MCP tool estate. (For runtime enforcement you'd
> pair it with a gateway.)

## Why a graph (and what the alternatives are)

Discovery (“find a tool like X”) is well served by vector search. Embedding
search **alone**, however, cannot give deterministic reachability or
blast-radius answers — `Agent → Tool → Resource → Policy` is a many-to-many
join across four node types, not a similarity problem.

Several structured-metadata approaches *can* answer the same questions if
given the same edges:

- OPA/Rego over a JSON-authored ACL model
- A relational schema with `(agent, tool, resource, policy)` joins
- A resource-tagged tool registry with namespace + grant tables

toolgraph is one implementation of that model, optimised for: idempotent
crawl-then-author flows, explainable paths returned as first-class output,
and exposing the same queries over MCP so an agent can audit the registry
through the protocol it was crawled from. It does **not** claim "only a
graph can answer this." It claims the graph form makes the authoring +
audit ergonomics simple, and that embedding-only registries cannot.

Every edge here is either a crawled fact or an operator-authored assertion;
nothing is LLM-guessed.

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

New to toolgraph? Start with the Korean beginner guide:
[`docs/ko-beginner-guide.md`](docs/ko-beginner-guide.md).

For the context-engineering and tool-selection roadmap, see
[`docs/context-engineering-tool-selection-report.md`](docs/context-engineering-tool-selection-report.md).
Contract-shaping decisions are recorded in [`docs/adr/`](docs/adr/README.md).

```bash
# 1. start Neo4j (pinned 5.26 community)
docker compose up -d

# 2. install
uv sync --extra dev
cp .env.example .env            # defaults match docker-compose

# 3. run the end-to-end demo (crawls the real filesystem MCP server)
bash scripts/demo.sh

# (or use the Makefile shortcuts: `make` lists targets, e.g. `make up`,
#  `make demo`, `make demo-public`, `make test`, `make lint`)

# or the cross-server demo: 7 real public servers (npm + uvx), and a secret
# reachable via tools on two different servers (filesystem AND git)
bash scripts/demo-public.sh
```

The cross-server demo is the headline: `ci-bot` has no filesystem read grant,
yet `unsafe-tools ci-bot` flags it — because `git_show` on a *different* server
reaches the same governed secret. The graph form makes the cross-server path
explicit without per-server bookkeeping; the same authored edges in a
relational or OPA model would catch it too, but operators would have to
write the join themselves.

The same wedge appears for **proxy/surfacing servers**. `examples/servers-gate.yaml`
crawls memtomem + memtomem-stm (a docs proxy whose `surfacing` engine injects
results from the operator's memtomem LTM into every proxied response — see
`memtomem_stm/proxy/manager.py:674`). `examples/governance-gate.yaml` flags
an agent whose only grants are on proxied docs tools (`langchain__*`,
`langfuse__*`) — because surfacing makes every such call indirectly read the
operator's personal LTM namespace, which their descriptions never mention.
Run with:

```bash
uv run toolgraph crawl --servers examples/servers-gate.yaml
uv run toolgraph ingest-manifest --governance examples/governance-gate.yaml
uv run toolgraph check-access helper "memtomem-stm::langchain__search_docs_by_lang_chain"
```

Or step by step:

```bash
uv run toolgraph check                                   # verify Neo4j connectivity
uv run toolgraph init-schema                             # uniqueness constraints
uv run toolgraph crawl --servers examples/servers.yaml   # crawl -> load factual graph
uv run toolgraph ingest-manifest --governance examples/governance.yaml
# refs to tools no server currently exposes emit a non-fatal DRIFT notice;
# add --strict-drift to reject the manifest instead (CI gating, ADR-0002)

# positive-reachability queries
uv run toolgraph unsafe-tools public-bot          # violations only (default)
uv run toolgraph unsafe-tools public-bot --all    # + operator-declared exceptions
uv run toolgraph check-access public-bot read_file
uv run toolgraph blast-radius "file:///private/secrets.env"

# negative-truth / drift queries — what the graph DOESN'T say but should
uv run toolgraph unmapped-tools                     # granted tools w/ no READS/WRITES yet
uv run toolgraph orphan-policies                    # policies no agent currently reaches
uv run toolgraph unbacked-edges                     # READS/WRITES/GOVERNED_BY w/ no evidence
uv run toolgraph unbacked-edges --include-grants    # also audit CAN_CALL grants
uv run toolgraph drift                              # tools w/ governance but no live EXPOSES
```

### Distinguishing what the graph doesn't know

- `check-access` verdicts include `AGENT_NOT_FOUND` (the agent string matches
  no Agent node — typo or deleted agent) and `TOOL_NOT_FOUND`. The two are
  not collapsed into `NOT_GRANTED`, so an operator iterating on names can
  tell which case they're in. Every response carries `agent_found: bool`
  and `found` for the tool — `True`/`False` when tool resolution ran, and
  `null` on the `AGENT_NOT_FOUND` branch (the tool was not looked up).
  Callers should use `result.get("found")` so the three-state shape doesn't
  trip indexing.
- When `verdict: "DENY"`, the response also carries
  `all_authorized: bool` — `true` when every `deny_evidence` row is
  `authorized_but_governed` (an operator-declared exception). Lets a caller
  detect "DENY only because of by-design grants" without aggregating per-row
  classifications.
- `blast-radius` returns `found: false` when the node argument matches no
  Resource or Policy. A typo'd URI is no longer indistinguishable from
  `impacted: []`. `blast-radius` accepts a URI-shaped argument as either a
  resource URI or a policy id — if the lexical URI pattern matches no
  Resource node, it retries against Policy ids before declaring not-found
  (so a policy id like `urn:secret` or `mailto:ops` works as a node).
- `unsafe-tools` (CLI) and the `unsafe_callable_tools` MCP tool both
  default to `classification='violation'` only; pass `--all` (CLI) or
  `include_authorized: true` (MCP) to see operator-declared exceptions
  too. Both surfaces also carry `agent_found: bool` so a typo'd agent
  doesn't look like an agent with zero unsafe tools.

### Result classification

`unsafe-tools` and `check-access` results carry a `classification`:

- `violation` — the agent reaches a DENY-governed resource and the operator
  has not declared the reach intentional.
- `authorized_but_governed` — operator declared `expected_exceptions` in the
  manifest covering this (agent, tool, resource?, policy); the row carries
  the operator's stated `exception_reason`.

This replaces the bare "unsafe" framing: review tooling can filter to
`violation` rows and stop drowning reviewers in by-design grants.

### Provenance on every edge

Each edge — crawled or authored — carries `source` (`crawled` |
`operator_asserted` | `inferred`), `confidence` (`high` | `medium` | `low`),
and `evidence` (free-text pointer: file:line, URL, ticket id, commit SHA).
Queries surface this on every result so a reviewer can see whether a DENY
verdict rests on a crawled fact or an unverified assertion:

```yaml
data_access:
  - tool: "memtomem-stm::langchain__search_docs_by_lang_chain"
    resource: "memory:claude:toolgraph"
    mode: READS
    provenance:
      source: inferred
      confidence: high
      evidence: "memtomem_stm/proxy/manager.py:674 (_apply_surfacing)"
```

`governed_by` bindings carry their own provenance — *why* this resource is
under this policy, which is often a different citation from the data-flow
edge. Bare-string entries are still accepted (no evidence, equivalent to
the old shape); add an object item to cite the binding's source:

```yaml
governed_by:
  "memory:claude:toolgraph":
    - policy: operator-ltm-confidentiality
      provenance:
        source: operator_asserted
        evidence: "~/.memtomem/config.json (namespace.rules)"
  "file:///srv/app/.env": [secrets-deny]   # bare string — no evidence
```

When a DENY fires via a resource path, the result row includes both
`provenance` (the data-flow edge that put the row in front of you) and
`policy_provenance` (the binding evidence, attached only when the author
cited one).

## toolgraph as an MCP server

toolgraph exposes its governance and audit queries as MCP tools, so an agent
can ask the registry about itself over the same protocol toolgraph crawls:

```bash
uv run toolgraph serve            # stdio
uv run toolgraph serve --http     # streamable-http
```

Tools: `check_access`, `unsafe_callable_tools`, `blast_radius`,
`unmapped_tools`, `orphan_policies`, `unbacked_edges`, `drifted_tools`.

## Inputs

- `servers.yaml` — MCP servers to crawl (stdio `command`/`args`, or http `url`).
- `governance.yaml` — authored agents, policies, `CAN_CALL` grants, `READS/WRITES`
  data access, `GOVERNED_BY` mappings, and `expected_exceptions` for intentional
  DENY-zone reach. Per-edge `provenance` is optional everywhere. See `examples/`.

Two reference exhibits:

- `examples/servers-gate.yaml` + `governance-gate.yaml` — the 2-server
  wedge demo (memtomem + memtomem-stm). Proves toolgraph composes
  cross-server reach a vector registry of tool descriptions cannot.
- `examples/servers-fleet.yaml` + `governance-fleet.yaml` — a 7-server
  fleet (the two above + filesystem, fetch, memory, sequential-thinking,
  time) with minimal but real governance. Validates that per-server
  authoring is minute-scale, not hour-scale. (Caveat: the original
  single-pass took ~48s because the author had already read the
  surfacing-engine source for the gate exhibit; an unfamiliar operator on
  unfamiliar servers should plan for 2–5 minutes per server.)

## Layout

```
toolgraph/
  crawler/   connect to MCP servers (stdio + streamable-http), enumerate tools/resources
  graph/     Neo4j driver, schema/constraints, idempotent loader, governance/audit queries
  manifest/  parse + ingest authored governance (idempotent)
  server/    FastMCP server exposing the queries as MCP tools
  cli.py     crawl / ingest-manifest / check-access / unsafe-tools / blast-radius /
             unmapped-tools / orphan-policies / unbacked-edges / drift / serve / reset
```

## Tests

```bash
uv run pytest          # uses a real Neo4j via testcontainers (no Cypher mocking)
```

## Scope (MVP)

This is a deliberately thin, thesis-validating MVP. **In:** factual crawl +
authored governance + three positive-reachability queries + four
negative-truth/drift queries + classification + per-edge provenance
(including per-`governed_by`-binding) + exception annotations + `found`
flags on `check-access` and `blast-radius` so unknown nodes are
distinguishable from empty results.

Idempotency is fully declarative: `ingest-manifest` re-applies authored
edges (CAN_CALL/READS/WRITES/GOVERNED_BY) AND prunes Agent + Policy nodes
the manifest no longer mentions, so renaming a policy in YAML does not
leave the old Policy node behind. Stale `prov_*` properties on Policy
nodes from earlier shapes are also cleaned at re-ingest.

Expected-exception validation enforces a full reachability invariant —
the operator can't declare an exception that fails to classify any row
(typo'd agent, no CAN_CALL grant, resource not governed by the policy,
or a wildcard exception with no tool→policy path are all caught at
ingest with a clear warning).

**Still deferred** (no plan to start until use proves them out):
LLM-inferred dependency/version edges, vector search, an official-registry
importer, and runtime enforcement. toolgraph remains an analyzer; the
runtime-enforcement wedge belongs to gateways like Kong.

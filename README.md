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

New to toolgraph? Start with the first-time user guides:
[`docs/beginner-guide.md`](docs/beginner-guide.md) (English) or
[`docs/ko-beginner-guide.md`](docs/ko-beginner-guide.md) (Korean).

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
`memtomem_stm/proxy/manager.py::ProxyManager._apply_surfacing`).
`examples/governance-gate.yaml` flags
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
uv run toolgraph destructive-unsafeguarded          # destructive-hinted tools w/ no policy path
uv run toolgraph annotation-contradictions          # authored WRITES vs readOnlyHint claims
uv run toolgraph audit-report                       # whole-graph operational audit summary
uv run toolgraph audit-report --format markdown     # human-readable audit report

# selector surface (ADR-0005) — batch, deterministic, no relevance scoring
uv run toolgraph rank-features planner git_status read_file export_data
uv run toolgraph eligible-tools planner git_status read_file --profile strict
uv run toolgraph selection-explain planner git_status

# advisory run artifact for syncmill/tracegraph correlation
uv run toolgraph preflight planner git_status read_file \
  --profile review --run-id "$RUN_ID" --out preflight.json

# human review of Tracegraph T3 findings (no Neo4j or policy mutation)
uv run toolgraph review-candidates list candidates.json
uv run toolgraph review-candidates annotate candidates.json CANDIDATE_ID \
  --status accepted --reviewer operator --note "manual policy review approved"
```

`preflight` wraps the same deterministic selector rules and stamps the result
with a bracketed `graph_generation`. It remains advisory: rejected candidates
produce `decision: advisory_warn` and exit 0. Unknown agents produce
`unresolved_identity`, never a false allow. Resource URIs in evidence are
scrubbed of userinfo and query strings before the artifact is written. Pass
`--features` only when the consumer needs the full selector feature rows.
SyncMill P3.1 may enforce this evidence before orchestration, but the producer
artifact and Toolgraph exit-code contract remain advisory (ADR-0008).

`review-candidates` is the G3 human-review boundary (ADR-0009). It consumes
Tracegraph's body-free schema-v1 report and projects only `run_id`, versioned
pattern identity, qualified `tool_key`, and the analyzed artifact digest.
Unknown additive v1 fields are discarded; unknown major versions fail closed.
The original report is never edited. `annotate` appends `open` / `accepted` /
`dismissed` events to `<REPORT>.toolgraph-review.json`, bound to the SHA-256 of
the exact report bytes and written under a local lock with atomic replacement.
The sidecar path is resolved before load, lock, and replacement so symlink
aliases cannot split history. Successful replacement fsyncs both the file and
its parent directory where the platform supports it.
Local review notes may cite paths but reject credential-shaped content.

Candidate ids use the same exact-tuple UUIDv5 as SyncMill's merged
`board import-review-candidates` consumer (SyncMill PR #57), so the two human
review surfaces can be correlated without sharing storage. Their states remain
independent: Toolgraph `accepted` is only a policy-review disposition. It does
not complete a SyncMill board item, modify governance, change selector output,
or increment `graph_generation`.

### Exit codes

Per [ADR-0003](docs/adr/0003-cli-output-and-exit-code-contract.md) this table
is a contract: query commands reserve stdout for their JSON (diagnostics go
to stderr) and exit 0 by default — gating is opt-in.

| Command | Exit code |
| --- | --- |
| query commands (default) | 0 always — advisory, even on DENY/violations |
| `unsafe-tools` / `rank-features` / `eligible-tools` / `selection-explain` (unknown agent) | 1 — `AGENT_NOT_FOUND` printed to stderr |
| `preflight` | 0 for advisory allow, warn, and unresolved identity; operational/argument errors are non-zero |
| `review-candidates list/annotate` | 0 on valid JSON output · 1 on invalid/stale artifacts or I/O failure · 2 on invalid CLI arguments |
| `ingest-manifest` (manifest REJECTED) | 1 |
| `crawl` (any server failed) | 1 |
| `check-access --fail-on-deny` | 1 on `DENY` with `all_authorized: false` · 2 on `AGENT_NOT_FOUND` / `TOOL_NOT_FOUND` / `AMBIGUOUS_TOOL` · 0 otherwise (`ALLOW`, `NOT_GRANTED`, authorized-only `DENY`) |
| `unsafe-tools --fail-on-violation` | 1 when any `violation` row remains in the printed output (with or without `--all`) |

A typo'd name exits 2 under `--fail-on-deny` so it can't pass a CI gate as a
clean run.

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
and `evidence` (free-text pointer: URL, ticket id, commit SHA, source
location — prefer symbol anchors like `path/to/file.py::Class.method` over
`file:line`, which rots as the cited source evolves).
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
      evidence: "memtomem_stm/proxy/manager.py::ProxyManager._apply_surfacing -> memtomem_stm/surfacing/engine.py::SurfacingEngine.surface"
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

### Tool annotations are self-claims, not facts

The crawler stores MCP tool annotations (`readOnlyHint`, `destructiveHint`,
`idempotentHint`, `openWorldHint`) as Tool-node properties on the same crawl
pass that owns the node, and clears any hint the server stops sending
([ADR-0006](docs/adr/0006-crawl-tool-annotations-as-self-claims.md)).

Their provenance is `crawled` / **`medium`** — deliberately one tier below
EXPOSES/PROVIDES (`high`). "The server advertised this tool" is
protocol-observable; "this tool is read-only" is the server's *own unverified
claim about itself*, and a lying or sloppy server can hint anything. Letting
hints launder into the same confidence tier as protocol facts would defeat the
provenance model.

Annotations never auto-create READS/WRITES/GOVERNED_BY edges — they only feed
two audit queries that join self-claims against authored truth:

- `destructive-unsafeguarded` — tools hinting destructive with no
  GOVERNED_BY path at all (direct or via any resource they touch). An
  authoring priority queue at zero authoring cost, not a verdict.
- `annotation-contradictions` — authored WRITES edges on tools hinting
  `readOnlyHint: true`. One side is wrong; each row cites both provenances
  (the hint's crawl pass, the authored edge's evidence pointer) and the
  operator picks the stronger evidence.

## Selector surface (deterministic, ADR-0005)

toolgraph can serve tool selection as a context-engineering layer: shrink a
candidate catalog deterministically *before* any relevance ranker sees it
([ADR-0005](docs/adr/0005-selector-surface-boundary.md), background in
[the report](docs/context-engineering-tool-selection-report.md)). Three
batch-first entry points (CLI + MCP) run a fixed number of queries
regardless of candidate count — per-candidate `check-access` calls scale
linearly, which is unacceptable on the online path:

- `rank-features AGENT CANDIDATES…` — per-candidate facts in input order:
  resolution (`found` / `ambiguous` / `tool_key`), grant, verdict, DENY
  classification + evidence paths, drift, mapping, evidence coverage, the
  four annotation self-claims, and a **rule-based** `risk_score` from this
  fixed table (first match wins): `1.0` DENY violation · `0.8` drifted ·
  `0.6` unmapped · `0.4` unbacked load-bearing edge · `0.2`
  authorized_but_governed · `0.0` otherwise.
- `eligible-tools AGENT CANDIDATES… --profile strict` — hard filter with
  reject reasons and policy-evidence paths. Profiles are named rule sets,
  not learned thresholds:

  | Profile | Use | Rejects |
  | --- | --- | --- |
  | `strict` (default) | production agents | everything but a clean, fully-authored ALLOW: not-found / ambiguous / not-granted / any DENY / drifted / unmapped |
  | `review` | human-in-the-loop | not-found / ambiguous / not-granted / DENY violations / drifted (operator exceptions and unmapped pass) |
  | `explore` | offline eval / sandbox | not-found / ambiguous / not-granted / drifted only |

- `selection-explain AGENT TOOL` — compact human-readable reasons for one
  decision, for operators and end users.

Hard boundary: no relevance scoring, no learning, ever — those belong to the
consumer. A learned model may rerank the eligible list but may **never
override a hard reject** (`NOT_GRANTED`, `violation`, drifted). Ambiguous
bare names are never auto-resolved — the consumer must re-submit the
server-qualified key. An unknown agent returns `agent_found: false`
(CLI: `AGENT_NOT_FOUND` on stderr, exit 1): selection should abort, because
a typo'd agent is a context-construction error, not an empty catalog.

MCP responses carry `graph_generation` (ADR-0004) so consumers can cache
features per graph state with one integer comparison.

### Wiring the shipped consumer (memtomem-stm)

The first consumer of this surface is the memtomem-stm proxy
(memtomem-stm#465): at startup it consults `eligible_tools` +
`rank_features` over MCP stdio (`toolgraph serve`) and feeds the verdicts
into its tool-exposure hard filter. The gate exhibit carries the consumer's
default `agent_id` (`stm-proxy`) as a wiring fixture, so the consult path
is demoable against the exhibit graph:

```bash
uv run toolgraph eligible-tools stm-proxy \
  "memtomem-stm::langchain__search_docs_by_lang_chain" --profile strict
# -> rejected: DENY_GOVERNED (fail closed — the consumer's production default)
uv run toolgraph eligible-tools stm-proxy \
  "memtomem-stm::langchain__search_docs_by_lang_chain" --profile review
# -> eligible (the operator-declared exception passes under review)
```

Live wiring differs from the exhibit in one important way: the proxy builds
candidate refs as `"<upstream-connection-name>::<raw-tool-name>"` from its
own upstream config, while the exhibit crawls the proxy itself (server
`memtomem-stm`, prefixed tool names like `langchain__…`). For a live
deployment, crawl the proxy's *upstreams* directly so graph server names
match the STM connection keys — or map them in the consumer's config:

```json
"toolgraph": {
  "enabled": true,
  "agent_id": "stm-proxy",
  "query_profile": "strict",
  "server_name_map": { "docs-langchain": "langchain-docs" }
}
```

(`"docs-langchain"` is the STM upstream connection key; `"langchain-docs"`
is the name that upstream was crawled under in this graph.) Refs that don't
resolve come back `TOOL_NOT_FOUND` and are handled by the consumer's
`on_tool_not_found` knob — if every candidate is rejected as not-found, the
name mapping (not governance) is what's wrong.

## toolgraph as an MCP server

toolgraph exposes its governance and audit queries as MCP tools, so an agent
can ask the registry about itself over the same protocol toolgraph crawls:

```bash
uv run toolgraph serve            # stdio
uv run toolgraph serve --http     # streamable-http
```

Tools: `check_access`, `unsafe_callable_tools`, `blast_radius`,
`unmapped_tools`, `orphan_policies`, `unbacked_edges`, `drifted_tools`,
`destructive_unsafeguarded`, `annotation_contradictions`, `rank_features`,
`eligible_tools`, `selection_explain`.

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
             unmapped-tools / orphan-policies / unbacked-edges / drift /
             destructive-unsafeguarded / annotation-contradictions /
             rank-features / eligible-tools / selection-explain / serve / reset
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

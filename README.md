# Toolgraph

> 🚧 **Alpha** — APIs and defaults may change between `0.x` minor releases.
> Feedback and issue reports are especially welcome:
> [Issues](https://github.com/memtomem/toolgraph/issues) ·
> [Discussions](https://github.com/memtomem/toolgraph/discussions)
>
> Toolgraph is an advisory control-plane tool, not a runtime security boundary.

A graph-native registry for MCP tools. It crawls MCP servers into a local Ladybug or shared Neo4j graph,
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
> risk/impact analyzer and policy compiler for your MCP tool estate. Runtime
> enforcement consumes its product-neutral policy bundle in a gateway;
> memtomem-stm is the first reference consumer, not a required dependency.

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

For before/after policy decisions and local regression checks, see the
[policy impact guide](docs/policy-impact.md).

For observed-usage grant proposals and provenance checks, see the
[policy proposal guide](docs/policy-propose.md).

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

New to Toolgraph? Start with the first-time user guides:
[English](https://github.com/memtomem/toolgraph/blob/main/docs/beginner-guide.md) or
[Korean](https://github.com/memtomem/toolgraph/blob/main/docs/ko-beginner-guide.md).
To run the same path one step at a time with the outputs in front of you, open the
[first-run notebook](https://github.com/memtomem/toolgraph/blob/main/docs/notebooks/01-first-policy-bundle.ipynb).

For the context-engineering and tool-selection roadmap, see
the [context-engineering report](https://github.com/memtomem/toolgraph/blob/main/docs/context-engineering-tool-selection-report.md).
Contract-shaping decisions are recorded in the
[ADRs](https://github.com/memtomem/toolgraph/tree/main/docs/adr).
Maintainers should follow the [release runbook](https://github.com/memtomem/toolgraph/blob/main/docs/releasing.md).

### Install

Ladybug, the recommended local backend, installs with Toolgraph. Releases up to
0.2.0 installed it through the `ladybug` extra, which still resolves, so these
commands work for every release. Ladybug ships wheels for macOS 15+, current
manylinux and musllinux, and Windows; elsewhere pip builds it from source.

```bash
uv tool install "toolgraph[ladybug]"
# or: python -m pip install "toolgraph[ladybug]"
```

That puts `toolgraph` on your `PATH`; run `uv tool update-shell` once if `uv`
says its bin directory is not there yet.

You can also inspect the CLI without a persistent install:

```bash
uvx --from "toolgraph[ladybug]" toolgraph --version
```

<details>
<summary>From source</summary>

```bash
git clone https://github.com/memtomem/toolgraph.git
cd toolgraph
uv tool install --from '.[ladybug]' toolgraph
```

</details>

### First policy bundle

Once `toolgraph` is on your `PATH`, no Docker, Node.js or clone is required:

```bash
toolgraph example init toolgraph-quickstart
cd toolgraph-quickstart
toolgraph init
toolgraph crawl --servers servers.yaml
toolgraph ingest-manifest --governance governance.yaml --strict-drift
toolgraph eligible-tools vibe-coder \
  policy-gateway::read_note policy-gateway::publish_note --profile review
toolgraph selection-explain vibe-coder policy-gateway::publish_note --profile review
toolgraph policy compile --agent vibe-coder --profile review \
  --output .toolgraph/policy-bundle.json
```

`servers.yaml` declares the whole fleet, and its `servers` key is required:
`servers: []` is the explicit empty fleet (default pruning then retires
every crawled server; `--no-prune` keeps them), while a blank file or one
without the key exits 1 before any server is contacted (ADR-0014).

Each server's catalog is capped at 10,000 records and 4 MiB of retained
record JSON (`--max-catalog-records`, `--max-catalog-bytes`; ADR-0016). A
server over the cap fails its crawl rather than loading partially. The cap
bounds what Toolgraph keeps after the MCP SDK has parsed a page, not what
the SDK buffers: with mcp 2.3.0, SSE events are limited to 1 MiB each, while
stdio lines and streamable-HTTP JSON bodies are not limited.

For `ingest-manifest --dry-run --json` and `doctor`, see the
[preview and diagnostic requirements](docs/policy-propose.md#preview-and-diagnostic-requirements),
including Neo4j catalog permissions and Ladybug session serialization.

Ladybug is the local single-process backend. Toolgraph commands own the DB
sequentially; gateways read only the generated JSON bundle. Use Neo4j 5.7 or
later for a shared service or multiple concurrent processes. The Neo4j driver
is the `neo4j` extra: install `toolgraph[neo4j]`. Against an older
server every command that opens the graph stops with `Neo4j server is older
than 5.7, the minimum Toolgraph supports; upgrade the server`. The beginner guides explain the
decision output and the memtomem-stm handoff.

For contributors and larger shared/fleet demos, clone the repository and run:

```bash
uv sync --group dev --extra neo4j
docker compose up -d --wait
cp .env.example .env
bash scripts/demo.sh
bash scripts/demo-public.sh
```

`.env` selects Neo4j. Do not also run `toolgraph init` in the same directory
unless both say the same thing: when `TOOLGRAPH_BACKEND` or `TOOLGRAPH_DB_PATH`
disagrees with `.toolgraph/config.json`, commands stop with a configuration
error instead of picking one silently. See
[Configuration and `.env`](#configuration-and-env) for the full order.

The compose file is development-only, uses a known password, and binds Neo4j
to loopback. It is not an authenticated production deployment. The demo
scripts themselves use disposable Ladybug databases and never reset your
configured graph.

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

Or step by step, on the base example: `examples/servers.yaml` crawls the public
filesystem MCP server through `npx` (network on first run), and
`examples/governance.yaml` authors two agents over it, `ops-agent` and
`public-bot`, with a DENY policy on `file:///private/secrets.env`. Run it from
the repository root against the backend `.env` selects:

```bash
uv run toolgraph check                                   # verify the configured backend (read-only)
uv run toolgraph init-schema                             # constraints (Neo4j) or tables (Ladybug); idempotent
uv run toolgraph doctor                                  # config source, connectivity, schema, graph state
uv run toolgraph crawl --servers examples/servers.yaml   # crawl -> load factual graph
uv run toolgraph ingest-manifest --governance examples/governance.yaml --dry-run   # preview; writes nothing
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
uv run toolgraph rank-features public-bot \
  filesystem::read_file filesystem::list_directory filesystem::write_file
uv run toolgraph eligible-tools public-bot \
  filesystem::read_file filesystem::list_directory --profile strict
uv run toolgraph selection-explain public-bot filesystem::list_directory --profile strict

# portable control-plane artifact for memtomem-stm or another MCP gateway
uv run toolgraph policy compile --agent public-bot --profile strict \
  --output policy-bundle.json
```

On this example `public-bot` reaches the governed secret through `read_file`,
so `unsafe-tools public-bot` returns one `violation` row and `audit-report`
reports `status: fail`. `list_directory` has a grant but no authored
READS/WRITES, so `strict` rejects it as `UNMAPPED`; `--profile review` lets it
through. The strict bundle compiled for `public-bot` therefore marks every
crawled tool rejected: most have no grant at all.

`policy compile` evaluates every currently exposed qualified tool, fingerprints
the crawled tool contract, and writes canonical JSON with private permissions
and atomic replacement. Each tool appears exactly once in the bundle; a
duplicated catalog row fails compilation instead of shipping. The bundle
carries a collision-safe
`graph_state: {instance_id, generation}` plus governance and catalog digests.
Toolgraph still does not claim that it blocked a call: a gateway consumes the
bundle, chooses review or strict behavior, and records the enforcement action
(ADR-0010). Re-run `ingest-manifest` before compilation when upgrading a graph
that predates the governance-digest field.

### Artifact workflows

These commands exchange files with other systems, so each needs an input the
step-by-step block does not produce:

- `<run-id>` is the run identifier of the orchestrator that will run the
  agent (SyncMill, or any stable string you use to correlate the run).
- `<control-plan.json>` is a body-free `toolgraph.control-plan` document
  written by an orchestrator such as SyncMill. The repository ships a valid
  sample at `contracts/fixtures/control-plan-v1.json`.
- `<report.json>` is a Tracegraph review-candidate report. The vendored sample
  is `contracts/tracegraph/v1.json`. `<candidate-id>` is printed by
  `review-candidates list`.

```bash
# advisory run artifact for syncmill/tracegraph correlation
uv run toolgraph preflight public-bot filesystem::read_file filesystem::list_directory \
  --profile review --run-id <run-id> --out preflight.json

# body-free orchestration topology + principal/tool-surface preflight
uv run toolgraph control-preflight <control-plan.json> \
  --profile review --out control-preflight.json

# human review of Tracegraph T3 findings (no graph or policy mutation)
uv run toolgraph review-candidates list <report.json>
uv run toolgraph review-candidates annotate <report.json> <candidate-id> \
  --status accepted --reviewer operator --note "manual policy review approved"

# accepted evidence -> current-policy work plan (still no policy mutation)
uv run toolgraph policy review-plan <report.json> --agent public-bot \
  --profile review --output policy-review-plan.json
```

The two samples name principals and tools that the base example graph does not
contain, so against it they produce `advisory_warn` and a review-plan row whose
current reason is `TOOL_NOT_FOUND`. They show the commands work; real inputs
come from the orchestrator and Tracegraph runs against your own fleet.

`preflight` wraps the same deterministic selector rules and stamps the result
with a bracketed `graph_generation`. It remains advisory: rejected candidates
produce `decision: advisory_warn` and exit 0. Unknown agents produce
`unresolved_identity`, never a false allow. Resource URIs in evidence are
scrubbed of userinfo and query strings before the artifact is written. Pass
`--features` only when the consumer needs the full selector feature rows.
With `--out` the artifact is written privately and atomically, and the command
prints a result envelope carrying `artifact_digest` — the SHA-256 of the exact
file bytes in the ecosystem's `sha256:<hex>` form, so a run report can record
the value as-is and a consumer can verify it with `sha256sum`. Digests cover
bytes, not parsed JSON: a consumer that re-serializes the artifact is hashing a
different document. Without `--out` the artifact itself goes to stdout. SyncMill P3.1 may enforce this evidence before orchestration, but the
producer artifact and Toolgraph exit-code contract remain advisory (ADR-0008).

`control-preflight` accepts a versioned, body-free upper-bound control graph.
It checks one start and end, reachability, acyclicity, terminal paths, bounded
model fan-out, and aggregate call/parallel limits. It then evaluates every
declared principal and qualified candidate tool inside one collision-safe
`graph_state` bracket. The canonical private result is still advisory and
exits successfully for policy or structural warnings; an orchestrator such as
SyncMill decides whether those warnings block a run. Prompts, outputs, patches,
credentials, commands, memory bodies, and filesystem paths do not belong in
either exchange artifact. Toolgraph does not execute, persist, resume, or
observe the declared workflow (ADR-0013).

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
Local review notes may cite paths but reject credential-shaped content: a
`<name>=`, or a `<name>:` followed by a space or a quote, where the name is a
token, password, secret, credential, API key or authorization, so `token:
rotated weekly` has to be rephrased while a versioned tool name such as
`srv::get_token:v2` is fine. Reviewer names and the report's run ids and tool
keys are checked the same way.
If a genuine parent-directory fsync error occurs after replacement, the event
remains successful and visible while a durability warning is written to stderr;
retrying would append a duplicate event.

Candidate ids use the same exact-tuple UUIDv5 as SyncMill's merged
`board import-review-candidates` consumer (SyncMill PR #57), so the two human
review surfaces can be correlated without sharing storage. Their states remain
independent: Toolgraph `accepted` is only a policy-review disposition. It does
not complete a SyncMill board item, modify governance, change selector output,
or increment `graph_generation`.

`policy review-plan` is the explicit next step after acceptance (ADR-0012).
It selects only currently accepted candidates, evaluates their qualified tool
keys against one agent/profile, binds the result to the current graph instance
and generation, and writes a private atomic work-plan artifact. It deliberately
sets `decision_mode: "human_required"` and `automatic_change: false`: an
operator still edits the governance manifest, runs `ingest-manifest`, reviews
the resulting decision/blast radius, and then runs `policy compile`. Trace
evidence never grants, denies, or creates an exception by itself.

### Configuration and `.env`

The runtime config file is chosen in this order:

1. `toolgraph --config PATH <command>` (a global option, before the
   subcommand). A file that is missing or unreadable is a usage error, exit 2.
2. `TOOLGRAPH_CONFIG`.
3. `.toolgraph/config.json` in the working directory, which `toolgraph init`
   writes.

Besides the required `"schema_version": 1`, the file holds only the backend
and the Ladybug database path. With
`--config`, the file decides both and `TOOLGRAPH_BACKEND` / `TOOLGRAPH_DB_PATH`
are ignored. Without it, either variable fills in a value the file does not
have, and a variable that disagrees with the file stops the command (exit 1):

```text
ERROR: conflicting backend configuration (.toolgraph/config.json): TOOLGRAPH_BACKEND=neo4j but the config file says ladybug. Pass --config to choose the file, or unset the variable.
```

The `TOOLGRAPH_DB_PATH` form reads `TOOLGRAPH_DB_PATH=... but the config file
says ...`. Backend names are compared ignoring case and surrounding spaces, and
paths after resolving them, so equivalent spellings are not a conflict.
`--help` still works under a conflict, and `doctor` reports it as a FAIL.
`toolgraph init` is not blocked, because it writes the file that settles the
question, but plain `init` writes `ladybug`: with `TOOLGRAPH_BACKEND=neo4j`
still set, the conflict remains. Use `init --backend neo4j`, or remove the
variable. When it comes from `.env`, unsetting it in the shell is not enough:
the next command loads it again, so edit `.env`.

With no config file and no `TOOLGRAPH_BACKEND`, nothing names a backend and
Toolgraph does not guess one (ADR-0017). A command that opens the graph exits
1 with ``ERROR: no backend configured: run `toolgraph init`, or set
TOOLGRAPH_BACKEND to ladybug or neo4j``. `NEO4J_*` variables alone do not select
Neo4j; they configure it once it is selected. A `--config` file without a
`backend` field is refused the same way, naming the file. `toolgraph init`
defaults to Ladybug, with its database at `.toolgraph/toolgraph.lbug`.

`NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`, and the default
input paths `TOOLGRAPH_SERVERS` / `TOOLGRAPH_GOVERNANCE` (used when `crawl
--servers` / `ingest-manifest --governance` is omitted) always come from the
environment, with or without `--config`.

The CLI loads `.env` from the working directory before every subcommand,
`serve` included. It never overrides a variable that is already set, and a
`TOOLGRAPH_CONFIG` inside it is honored. Importing `toolgraph` as a library
does not read `.env`.

### Exit codes

Per [ADR-0003](https://github.com/memtomem/toolgraph/blob/main/docs/adr/0003-cli-output-and-exit-code-contract.md) this table
is a contract: query commands reserve stdout for their JSON (diagnostics go
to stderr) and exit 0 by default — gating is opt-in.

| Command | Exit code |
| --- | --- |
| any command (usage error) | 2 — bad flag or argument, unknown `--profile`, more than 4096 candidates, a `--config` file that is missing or unreadable |
| commands that read or write the graph | 1 with one `ERROR:` line on stderr when no backend is configured, for a configuration conflict, an unreadable config file, an unavailable backend, rejected Neo4j credentials, a `NEO4J_DATABASE` the Neo4j server does not have, a Neo4j server older than 5.7, or Neo4j selected without the `neo4j` extra. A command that only reads the graph (not `check`) also exits 1 with one line on a Ladybug database whose schema is missing, telling you to run `toolgraph init-schema`, or was written by another schema version, telling you to upgrade Toolgraph or migrate the database; writers initialize a missing schema themselves. It does not create a missing Ladybug database either: it reports the backend as unavailable. (If another process deletes the file at the instant a read opens it, Ladybug recreates it empty; the schema check then refuses it.) Exceptions: `doctor` reports these as a FAIL in its own output; `init` writes the config file instead of reading it, so a missing backend, a conflict or an unreadable config file does not stop it; and `serve` refuses the configuration cases (no backend, a conflict, an unreadable config file) before serving, with the same line and exit 1, and meets the rest per tool call over MCP, an outage as a `backend_unavailable` result and the others as a `backend_misconfigured` result |
| query commands (default) | 0 always — advisory, even on DENY/violations |
| `unsafe-tools` / `rank-features` / `eligible-tools` / `selection-explain` (unknown agent) | 1 — `AGENT_NOT_FOUND` printed to stderr |
| `check` / `init-schema` | 0 · 1 when the backend cannot be reached or initialized |
| `init` | 0 · 1 when `--state-dir` cannot be created · 2 on a `--backend` other than `ladybug` / `neo4j` |
| `doctor [--json]` | 0 when no check FAILs (WARN does not) · 1 on any FAIL, including a configuration conflict or no configured backend |
| `crawl` | 1 on an invalid servers file (including one without a `servers` list), two servers claiming one name or identity (nothing loaded), or any server failing (the others still load) |
| `ingest-manifest` | 1 on an unreadable or invalid file · 1 when the manifest is REJECTED (graph unchanged) |
| `ingest-manifest --dry-run [--json]` | 0 when the manifest would apply · 1 when it would be rejected (the `--json` report is still printed) · 2 for `--json` without `--dry-run` |
| `reset` | 0 · 1 without `--yes` (the refusal goes to stdout) |
| `check-access --fail-on-deny` | 1 on `DENY` with `all_authorized: false` · 2 on `AGENT_NOT_FOUND` / `TOOL_NOT_FOUND` / `AMBIGUOUS_TOOL` · 0 otherwise (`ALLOW`, `NOT_GRANTED`, authorized-only `DENY`) |
| `unsafe-tools --fail-on-violation` | 1 when any `violation` row remains in the printed output (with or without `--all`) |
| `audit-report --fail-on-blocking` | 1 when `status` is `fail` (unsafe violations or drifted governed tools) |
| `preflight` | 0 for advisory allow, warn, and unresolved identity · 1 when the artifact cannot be built or written |
| `control-preflight` | 0 including advisory warnings · 1 on an invalid plan or an I/O failure |
| `policy compile` | 0 after atomic bundle replacement · 1 on unresolved identity/state or I/O failure |
| `policy review-plan` | 0 · 1 on an invalid or stale report, unresolved graph state, or I/O failure |
| `policy propose` | 0 · 1 on invalid input or a refused export · 2 for `--json` with `--output`, or `--min-would-block` below 1 |
| `review-candidates list/annotate` | 0 on valid JSON output · 1 on invalid/stale artifacts or I/O failure · 2 on invalid CLI arguments |
| `example init` | 2 when the destination already exists |

An error outside these classes exits 1 with a Python traceback instead of an
`ERROR:` line.

A typo'd name exits 2 under `--fail-on-deny` so it can't pass a CI gate as a
clean run.

Note for CI scripts: exit 2 is overloaded. Click uses 2 for usage errors (a
bad flag or parameter), and `--fail-on-deny` uses 2 for not-found/ambiguous
verdicts. The two are indistinguishable by exit code alone — a gate that must
tell them apart should also check stderr (verdict trips print the JSON verdict
on stdout first; usage errors print click's usage text).

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
  too. Neither lets a typo'd agent look like an agent with zero unsafe
  tools: the MCP tool carries `agent_found: false`, and the CLI prints
  `AGENT_NOT_FOUND` on stderr and exits 1.

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
([ADR-0006](https://github.com/memtomem/toolgraph/blob/main/docs/adr/0006-crawl-tool-annotations-as-self-claims.md)).

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
([ADR-0005](https://github.com/memtomem/toolgraph/blob/main/docs/adr/0005-selector-surface-boundary.md), background in
[the report](https://github.com/memtomem/toolgraph/blob/main/docs/context-engineering-tool-selection-report.md)). Three
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
features per graph state. Cache identity is the pair (`graph_instance_id`,
`graph_generation`), not the integer alone: generations restart with the graph.

A response also carries `graph_state_verified`. It is `true` when the read was
bracketed by a stable graph state, and `false` when the graph moved under every
retry — in which case all three state fields are null:

```json
{"graph_generation": null, "graph_instance_id": null,
 "graph_state": null, "graph_state_verified": false}
```

An unverified response is still the answer to your question, but it must not be
cached and must not be attributed to a graph state. Nothing enforces that for
you: `null` is a usable dictionary key and two unverified responses compare
equal on it. A consumer that turns eligibility into an authorization decision
should reject or retry rather than proceed on one.

If the graph backend is temporarily unavailable, every MCP tool returns
`isError: true` with a typed `structuredContent` envelope instead of exposing
driver-specific text:

```json
{
  "error_kind": "backend_unavailable",
  "retryable": true,
  "message": "Toolgraph backend is temporarily unavailable; retry later."
}
```

Only this exact discriminator is an availability signal.

A backend configuration problem that an operator has to fix returns
`isError: true` with a second envelope. `retryable` is always `false`:
waiting does not fix configuration.

```json
{
  "error_kind": "backend_misconfigured",
  "retryable": false,
  "cause": "neo4j_credentials_rejected",
  "message": "Toolgraph backend is misconfigured: Neo4j refused authentication; see the server log."
}
```

`cause` is one of `neo4j_extra_missing`, `neo4j_credentials_rejected`,
`neo4j_database_not_found`, `neo4j_server_too_old`, `ladybug_missing`,
`backend_corrupt`, `schema_not_initialized` and `schema_version_mismatch`, and
each has a fixed `message`. The two schema causes are Ladybug-only: every read
checks the schema first. The details the CLI prints,
such as the database file's path or the Neo4j status code, go to the server
log as one WARNING record without a traceback, and never into the result. If a failure's cause chain
holds both an outage and one of these causes, the result is
`backend_misconfigured`.

Validation, query-contract, and unexpected failures, and configuration
problems outside that list, keep the normal MCP error behavior and should
fail loud. Successful CLI output and Python result shapes are unchanged; only
the exception type surfaced on an outage differs (see below).

Outages are typed once at the backend seam — `driver.session()` and
`verify_connectivity()` raise `BackendUnavailableError` — so the server layer
never inspects driver-specific exception types. `retryable` is read from the
annotations the server actually advertises for that tool, rather than assumed:
every current tool is a pure graph read. Anything else fails closed to
`retryable: false` — a tool that does not advertise `readOnlyHint`, an unknown
tool, and any case where the annotations cannot be read at all. Treat
`retryable: false` as "do not assume a retry is safe", not as proof that the
operation writes.

**API change for embedders.** Because outages are typed at that seam, code
calling `driver.session()` or `verify_connectivity()` directly now sees
`BackendUnavailableError` where it previously saw `neo4j.exceptions`
`ServiceUnavailable`, `SessionExpired`, `ConnectionAcquisitionTimeoutError`, or
`DatabaseUnavailable`. Catch `BackendUnavailableError` (or its
`BackendLockedError` subclass) instead; the original driver exception is
preserved as `__cause__`. Neo4j authentication failures (`AuthError` and its
subclasses, plus the plain `ClientError` codes `Security.CredentialsExpired`
and `Security.AuthenticationRateLimit`), a configured database the server does
not have (the plain `ClientError` code `Database.DatabaseNotFound`) and a
server older than Neo4j 5.7
(the driver's notification-filtering `ConfigurationError`) raise
`BackendConfigurationError` from `driver.session()`,
`acquire_readonly_session()` and `verify_connectivity()`, again with the
driver error as `__cause__`; it is not an outage and not retryable. Other configuration and Cypher/client
errors are untouched, as is the still-raw `get_driver()` escape hatch. Note
that `is_backend_unavailable()` narrowed with it: it classifies only
`BackendUnavailableError`, so exceptions raised straight out of `get_driver()`
no longer answer yes. Route retry-sensitive calls through `driver.session()`.

### Wiring the shipped consumer (memtomem-stm)

The first consumer of this surface is the memtomem-stm proxy
(memtomem-stm#465): at startup it consults `eligible_tools` +
`rank_features` over MCP stdio (`toolgraph serve`) and feeds the verdicts
into its tool-exposure hard filter. The gate exhibit carries the consumer's
default `agent_id` (`stm-proxy`) as a wiring fixture, so the consult path
is demoable against the exhibit graph. Load it first with the gate demo's
`crawl` and `ingest-manifest` commands under [Quickstart](#quickstart):
ingesting `examples/governance.yaml` afterwards, as the step-by-step block
does, replaces the gate governance and removes `stm-proxy`.

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

All thirteen tools are pure graph reads and advertise `readOnlyHint` /
`idempotentHint`. Each one has a CLI counterpart, though names, flags and
result shapes do not always match:

| MCP tool | CLI command | Differences |
| --- | --- | --- |
| `check_access(agent, tool)` | `check-access AGENT TOOL` | Same verdict object. The CLI adds `--fail-on-deny`. |
| `unsafe_callable_tools(agent, include_authorized=false)` | `unsafe-tools AGENT [--all]` | MCP returns `{agent, agent_found, count, tools}`; the CLI prints the bare list and exits 1 on an unknown agent. The CLI adds `--fail-on-violation`. |
| `blast_radius(node)` | `blast-radius NODE` | Same object. |
| `unmapped_tools(only_granted=true)` | `unmapped-tools [--all]` | `--all` is `only_granted: false`. MCP `{count, tools}`, CLI list. |
| `orphan_policies()` | `orphan-policies` | MCP `{count, policies}`, CLI list. |
| `unbacked_edges(include_grants=false)` | `unbacked-edges [--include-grants]` | MCP `{count, edges}`, CLI list. |
| `drifted_tools()` | `drift` | MCP `{count, tools}`, CLI list. |
| `destructive_unsafeguarded()` | `destructive-unsafeguarded` | MCP `{count, tools}`, CLI list. |
| `annotation_contradictions()` | `annotation-contradictions` | MCP `{count, contradictions}`, CLI list. |
| `audit_report(include_grants=false)` | `audit-report [--include-grants]` | Same object. The CLI adds `--format markdown` and `--fail-on-blocking`. |
| `rank_features(agent, candidates)` | `rank-features AGENT CANDIDATES…` | Same object. |
| `eligible_tools(agent, candidates, profile="strict")` | `eligible-tools AGENT CANDIDATES… [--profile]` | Same object. |
| `selection_explain(agent, tool, profile="strict")` | `selection-explain AGENT TOOL [--profile]` | Same object. |

Every successful MCP result also carries `graph_generation`,
`graph_instance_id`, `graph_state` and `graph_state_verified` (see the
selector section above); of the CLI counterparts in this table, only
`audit-report` prints them, and only in its JSON format. For an unknown agent, the
selector tools return `agent_found: false` over MCP, where the CLI exits 1. An
unknown profile or more than 4096 candidates is a tool error carrying the
message over MCP and a usage error (exit 2) on the CLI.

The other commands have no MCP tool: `example init`, `init`, `init-schema`,
`check`, `doctor`, `crawl`, `ingest-manifest`, `reset`, `serve`, `preflight`,
`control-preflight`, `policy compile` / `review-plan` / `propose`, and
`review-candidates list` / `annotate`.

## Inputs

- `servers.yaml` — MCP servers to crawl (stdio `command`/`args`, or a `url` for
  `streamable-http`; `sse` is accepted for legacy servers that predate it).
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
  cli.py                the `toolgraph` command (commands: see the CLI/MCP table above)
  config.py             config file, environment and .env resolution
  health.py             `doctor` checks
  crawler/              connect to MCP servers (stdio, streamable-http, legacy sse), enumerate tools/resources
  manifest/             parse + ingest authored governance (idempotent, with --dry-run)
  graph/
    driver.py           backend lifecycle for Ladybug and Neo4j
    schema.py           Neo4j constraints / Ladybug tables and the schema version
    loader.py           idempotent load of crawl results
    queries.py          governance and audit queries
    selector.py         deterministic selector surface (ADR-0005)
    store.py            graph ports used by the policy services
  server/               MCP server exposing the queries as MCP tools
  preflight.py          advisory preflight artifact
  control_plan.py       control-plan validation and control-preflight
  policy_bundle.py      policy bundle compiler
  policy_review.py      accepted trace evidence -> policy review plan
  policy_propose.py     grant proposals from observed gateway usage
  review_candidates.py  Tracegraph review-candidate intake and annotations
  artifacts.py          canonical JSON, private atomic writes, digests
  artifact_safety.py    evidence scrubbing and artifact identity checks
  redaction.py          credential-free labels for endpoints and diagnostics
  models.py             pydantic models for crawl results and authored governance
  errors.py             errors whose message is safe to return to a caller
  uris.py               resource-URI canonicalization
  yaml_loader.py        fail-closed YAML loading
  quickstart/           files `toolgraph example init` copies out
contracts/              JSON Schemas for the exchanged artifacts, their fixtures, vendored Tracegraph contract
examples/               crawl targets and manifests for the repository demos (see examples/README.md)
scripts/                demos, smoke harnesses, release and dependency-audit helpers
docs/                   guides, ADRs, release runbook
tests/
```

## Tests

```bash
uv run pytest          # uses a real Neo4j via testcontainers (no Cypher mocking)
make test-unit         # the tests that need no Docker (-m "not neo4j")
```

## Scope

Toolgraph stays an advisory analyzer. **In:** factual crawl, authored
governance with per-edge provenance (including per-`governed_by`-binding),
reachability and audit queries over the result, violation vs operator-exception
classification, `found` flags so unknown nodes are distinguishable from empty
results, a deterministic selector, and portable artifacts (policy bundle,
preflight, control preflight, review plan) that other systems act on.

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

## Contributing, security, and license

- **Contributing:** [CONTRIBUTING.md](CONTRIBUTING.md) — development setup,
  pull-request conventions, and the Contributor License Agreement.
- **CLA:** [CLA.md](CLA.md). Signing is one-time per GitHub account per
  repository; the workflow comments on your first pull request with
  instructions.
- **Code of conduct:** [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) — applies to
  issues, pull requests and discussions.
- **Security:** [SECURITY.md](SECURITY.md) — please use private vulnerability
  reporting rather than a public issue.
- **License:** Apache License 2.0 — see [LICENSE](LICENSE).
  Copyright 2025-2026 DAPADA Inc. and memtomem contributors.

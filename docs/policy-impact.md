# Previewing policy impact without applying governance

`policy impact` answers which decisions change for **one agent and an explicit
list of tool references**. It reads the current graph, validates the entire
candidate replacement manifest, and evaluates proposed governance in memory.
There is no temporary ingest or rollback on the source graph.

## Review a change

Start with an initialized, crawled graph with ingested governance. Copy your
manifest to `proposed.yaml` and edit that copy. For the generated quickstart:

```sh
toolgraph policy impact proposed.yaml \
  policy-gateway::read_note policy-gateway::publish_note \
  --agent vibe-coder --profile review
```

The default Markdown report names newly eligible, newly rejected, unchanged,
and unresolved comparisons, plus before/after reasons and evidence. A change
to evidence or a rejection reason is shown even when eligibility stays the
same. Missing agents (including deliberately removed agents), missing tools,
and ambiguous bare names remain visible as unresolved; they are never a
successful no-regression result. Prefer qualified tool keys.

The default profile is `strict`; `review` and `explore` retain their established
selector meaning. `--strict-drift` also rejects a candidate manifest containing
drifted references, matching ingest's optional validation gate. The whole
manifest is validated even when only a few tools are selected for comparison.
A removed authored-only tool is evaluated as absent after its last edge is
removed, matching actual ingest pruning.

The command exits 0 for a complete comparison, even if it contains changes or
unresolved rows. Invalid input, missing graph provenance, repeated concurrent
changes, and capacity limits fail with exit 1. CLI syntax errors retain Typer's
exit 2 behavior. Nothing here asserts runtime gateway enforcement.

## Save and check regression expectations

```sh
toolgraph policy impact proposed.yaml \
  policy-gateway::read_note policy-gateway::publish_note \
  --agent vibe-coder --profile review --format json --output impact.json

toolgraph policy check impact.json --format json
```

The JSON artifact's proposed (`after`) decisions are the expectations. A check
against the unchanged baseline can therefore fail until the operator separately
reviews and applies the candidate manifest. Neither command applies it for you.
`policy check` exits 0 only when every scoped decision, rejection reason, and
resolved tool key matches; mismatch, unresolved input, or incompatible state
returns 1. This intentionally treats both a mismatch and an inability to check
as a failed CI gate. A completed JSON check has `passed: false` for a mismatch;
operational errors instead emit an error diagnostic without a result. Syntax
errors retain exit 2. The default Markdown output includes a comparison table
and both governance digests. It does not require evidence strings or risk
scores to stay identical.

The artifact binds graph instance, source generation, baseline and proposed
manifest digests, profile, scope and a scoped catalog fingerprint. Checking
allows generation/governance changes and reports both expected and current
governance digests. It refuses a different graph instance or changed resolution,
exposure or tool contract within the selected scope. Generate and review a new
artifact after intentional changes. For unexpected exposure changes, check
crawl/server status and restore the catalog before retrying. An unresolved
comparison cannot be used as a regression expectation, including when an agent
is newly introduced.

Expectations are operator-controlled local inputs, not signed evidence or
approval tokens. Checking them never grants authority. Use the explicit check
command as an opt-in CI regression gate. No PR comments or notifications are
posted automatically.

## Contracts and capacity

The [v1 schema](../contracts/policy-impact.schema.json) describes the complete
JSON report. Results are deterministically sorted by unique input reference;
provide a fixed `created_at` to the library for byte-repeatable output. The
scope accepts 1–4096 input references; duplicates are deduplicated after the
input-count check.

Each read attempt has a cumulative 100,000-row and 50,000,000-byte serialized
projection budget, including derived hypothetical evidence. JSON artifacts and
check results also have a 50,000,000-byte limit. Exceeding a budget rejects the
whole result; there is no silent truncation. These are retained-data limits,
not guarantees about database query work, driver buffering, or peak RSS.

Reads use the existing read-only backend session and physical-schema checks.
Ladybug readers serialize and require an existing initialized database. Neo4j
readers also need catalog-inspection permission; no schema initialization or
migration is performed. A strict graph-state bracket retries the full read
when governance/catalog state changes, then fails if it cannot get stable
state. The result describes that captured state, not a promise that the live
graph remains unchanged after the command returns.

Opaque agent/tool identities are validated and preserved. Evidence is scrubbed
for URI credentials/query data and credential-shaped free text. Display
escaping makes Unicode control/format characters and line/paragraph separators
visible, and distinguishes their escapes from literal backslash text. Executable
JSON identities are unchanged; evidence JSON still decodes to its original values.
Files written via `--output` use the private atomic writer (0600); stdout follows
the caller's storage permissions. Output cannot overwrite the input manifest,
including symlink or hardlink aliases. Existing structural dry-run and bundle
schemas are unchanged.

See [Proposed ADR-0015](adr/0015-policy-impact-preview.md) for the evaluation
boundary.

# ADR-0013: Body-free control plans are advisory topology and policy evidence

**Status:** Accepted
**Date:** 2026-08-11

## Context

An orchestrator needs to ask Toolgraph about an entire prospective run without
sending one unrelated request per agent. The useful governance questions are
whether the declared principals and qualified tool candidates are known and
eligible, and whether the proposed control topology has explicit, coherent
upper bounds. Prompts, work products, commands, credentials, and repository
paths are neither necessary nor appropriate control-plane inputs.

Toolgraph is not a durable workflow engine. Accepting a graph-shaped artifact
must not imply that Toolgraph executes nodes, owns checkpoints, proves runtime
conformance, or blocks calls.

## Decision

Toolgraph accepts `toolgraph.control-plan` schema v1 through
`toolgraph control-preflight PLAN`. The plan is a body-free upper-bound DAG with:

- producer and run identity;
- principals with qualified candidate tool keys;
- typed nodes and edges, including who decides each edge;
- per-agent-node call, parallelism, and timeout bounds; and
- run-level maximum agent calls, parallelism, and zero control cycles.

The producer rejects oversized inputs, forbidden body fields, credential-shaped
URIs, and absolute filesystem paths. Same-major additive fields are tolerated;
unknown major versions fail closed.

The structural linter reports topology and bound findings. All principal/tool
evaluations run inside one `with_graph_state(..., strict=True)` bracket. The
canonical, private `toolgraph.control-preflight` result binds the exact input
bytes by SHA-256 and contains a non-null `{instance_id, generation}` token.

Both `advisory_allow` and `advisory_warn` exit successfully. Only an invalid
input, unavailable graph, or failed artifact write returns a command failure.
Consumers own enforcement policy and must verify the plan digest, run identity,
complete evaluation set, and graph-state token before acting.

## Consequences

- A single graph snapshot covers all principals in one orchestration run.
- SyncMill can opt into a strict consumer gate without turning Toolgraph into
  the runtime data plane.
- The artifact proves declared topology and selector evidence, not actual node
  execution, checkpoints, idempotency, stopping behavior, or telemetry.
- The existing `toolgraph preflight` contract remains unchanged.
- Typed shared state, durable execution, runtime conformance, and OpenTelemetry
  trace correlation require later, independently versioned contracts.

## v1 exposure evidence and decision rules (2026-09-16)

Current producers always include `potential_exposures`. Each record joins an
eligible reader tool on a plan node to an eligible writer tool on a distinct,
downstream plan node. The record contains `reader_node`, `reader_principal`,
`read_tool`, `read_resource`, the corresponding `writer_node`, `writer_principal`,
`write_tool`, `write_resource`, and a shortest `witness_path`. These are potential
control/data paths, not proof that data was transferred. Resource fields are
redacted evidence labels; they are not opaque resource identity tokens.

For the current producer, `decision` is `advisory_warn` if any structural finding, missing agent, rejected
tool, nonempty exposure list, or `truncated: true` is present. Otherwise it is
`advisory_allow`. Both decisions exit successfully; consumers choose enforcement.
This extends the earlier decision rule: a plan whose tool evaluations all pass
can now warn because of potential exposure or incomplete exploration.

The search is bounded to 100 records and 64,000 bytes for the **sanitized**
exposure array serialized as UTF-8 with Python `json.dumps` default separators
and escaping. This is an array budget, not a bound on the entire preflight
artifact or a pretty-printed rendering. Witness paths contain at most 32 entries:
16 leading IDs, an omitted-nodes marker, and 15 trailing IDs for longer paths.
Pre-2026-09-16 main snapshots could emit 33 entries for long witness paths due
to a compression off-by-one. Those local artifacts exceed the now-documented
32-entry schema limit; regenerate them with the corrected producer. Decision
compatibility does not imply acceptance of that historical oversized evidence.
Traversal uses parent pointers, prunes branches with no reachable writer, and
limits cumulative forward node/edge visits across readers to 65,536 per graph
snapshot attempt. The initial reverse reachability pass is bounded by the plan's
4,096-node and 16,384-edge limits. A plan with no writer needs no forward search.

If a record, byte, or traversal limit stops exploration, `truncated: true` and
`total_candidate_pairs_found` are emitted. The latter is a **discovered lower
bound**, including candidates refused by output budgets; it is not an exhaustive
pair count or the emitted-record count. A traversal cutoff can report zero.
Missing `truncated` means false; missing exposure fields in legacy v1 artifacts
means that producer did not report this analysis, not that it proved no exposure.

These are additive v1 fields, explicitly typed by `control-preflight.schema.json`.
The schema does not enforce a cross-field mapping from evidence to `decision`:
legacy v1 results remain valid with either advisory decision, even when a newer
producer would choose differently. The current producer's mapping is covered by
producer tests. Consumers must not treat structural schema validation alone as
proof that a producer applied the current decision rules. No claim of unchanged
consumer behavior is made: consumers adopting exposure-aware results must review
their advisory enforcement policy, and absence of exposure data remains unknown.
The 64,000-byte limit is producer-enforced and tested at runtime; JSON Schema
does not measure serialized byte size.

The original `control-plan-v1.json` / `control-preflight-v1.json` fixtures retain
their exact prior bytes and semantics. A separate `control-plan-exposure-v1.json`
/ `control-preflight-exposure-v1.json` pair illustrates downstream exposure and
binds exact plan bytes. This control-preflight rule is separate from the
single-agent selection preflight's `unresolved_identity` decision described in
the ecosystem plan.

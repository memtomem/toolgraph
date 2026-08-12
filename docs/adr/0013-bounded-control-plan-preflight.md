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

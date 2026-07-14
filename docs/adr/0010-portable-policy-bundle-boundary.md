# 0010 — Portable policy bundle separates control plane from gateway data plane

Status: Accepted · Date: 2026-07-14

## Context

memtomem-stm already owns a mature MCP proxy lifecycle, while other gateways
may also need Toolgraph decisions. Putting proxy transports, retries, caches,
or authentication inside Toolgraph would duplicate that runtime and couple the
policy graph to one gateway product.

Generation alone is also an unsafe external cache key: reset or backend
reconstruction can produce the same number for a different graph.

## Decision

Toolgraph remains an advisory control plane and compiles
`toolgraph.policy-bundle` v1. The artifact is product-neutral, contains one
agent/profile's deterministic decisions for the currently exposed qualified
tool catalog, and fingerprints each crawled tool contract. Exact artifact
bytes are the deployment identity.

Every bundle carries `{instance_id, generation}`. `instance_id` persists for
the lifetime of one graph and changes after reconstruction. The existing
`graph_generation` field remains on preflight and MCP responses; the composite
`graph_state` is additive.

Toolgraph never opens an upstream call path while compiling or serving this
artifact. Consumers independently choose review/fail-open/strict behavior and
record the action they actually enforced. memtomem-stm is the first reference
consumer, not a privileged or schema-specific consumer.

## Consequences

- A standalone gateway can consume the same JSON Schema without importing the
  Toolgraph package.
- memtomem-stm can enforce list and call decisions without a database or
  Toolgraph process on the call hot path.
- Bundle compile fails rather than replacing the last good artifact when agent
  identity or governance state is unresolved.
- Runtime proxy features, authentication, rate limiting, and retry policy stay
  outside Toolgraph.

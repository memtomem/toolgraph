# 0001 — Node existence is a truth signal: prune every authored node type

Status: Accepted · Date: 2026-06-11

## Context

The completion round made node existence part of the query API: `check-access`
returns `AGENT_NOT_FOUND`, `blast-radius` returns `found: false` for unknown
nodes. To keep those signals honest, ingest Phase 2 prunes Agent and Policy
nodes the manifest no longer mentions.

Resource nodes were left out. `data_access` and `governed_by` MERGE Resource
nodes, ingest clears their edges on re-apply but keeps the node, and the
loader deliberately keeps Resource nodes too (they may be shared across
servers). Consequence: after an operator removes a resource from the manifest,
`blast-radius <uri>` still answers `found: true, impacted: []` — the same
lying-existence-signal class the Agent/Policy prune fixed.

## Decision

Every node type whose existence the API treats as truth gets the same
declarative lifecycle. At the end of ingest Phase 2, delete Resource nodes
that have **no PROVIDES edge and no authored edge** (after re-apply). Crawled
resources are never touched — PROVIDES keeps them alive; the loader remains
their owner.

## Consequences

- `blast-radius` `found` becomes trustworthy for resources, matching agents
  and policies.
- A resource referenced only by an old manifest disappears on re-ingest —
  intended: the manifest is the single source of authored truth.
- Any future authored node type (e.g. teams, environments) must ship with its
  prune rule in the same PR; "exists but means nothing" nodes are a bug class,
  not a backlog item.

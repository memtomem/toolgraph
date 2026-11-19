# 0004 — `graph_generation`: a monotonic id stamped at crawl/ingest

Status: Accepted · Date: 2026-06-11

## Context

Two consumers need to know *which graph state* an answer came from:

- A selector caching graph-derived features (drift, unmapped, unbacked) must
  invalidate when the graph changes — re-running every audit query per
  selection is too slow for an online path.
- Selection telemetry must be replayable: "the ranker saw these features"
  is only meaningful pinned to a crawl/ingest state.

Nothing in the graph today identifies its own revision.

## Decision

A single `GraphMeta` node, the singleton (`id: 'singleton'`), carries
`generation: int` (and `updated_at`).
Every successful `crawl` load and every successful `ingest-manifest`
increments it in the same transaction as the mutation. Query results that
feed the selector surface (and the MCP server responses) include
`graph_generation`. Failed/rejected operations do not increment.

Because Neo4j reads are read-committed, producers bracket a query with two
generation reads and retry when they differ.

Live MCP queries keep an availability-first fallback after bounded retry
exhaustion, but they no longer stamp a state onto the result. The earlier
fallback returned the last query result labelled with the freshest generation,
described as letting caches "self-heal"; it did the opposite. A result read at
generation 8 went out labelled 10, so a verdict computed before a permission
change could be cached under the generation that changed it, and outlive it.

An unbracketed read now returns `graph_generation`, `graph_instance_id` and
`graph_state` all null, with `graph_state_verified: false`. Successful reads
carry `graph_state_verified: true`, so an absent guarantee is never inferred
from an absent field.

The flag means one thing only: this read was bracketed by a stable graph state.
It is not a freshness, provenance or enforcement claim. **Consumers must treat
an unverified response as non-cacheable and must not attribute it to a graph
state.** A null generation is not self-enforcing -- `None` is a perfectly good
dictionary key, and two unverified responses compare equal on it -- so the
obligation is on the consumer, and a consumer that turns eligibility into an
authorization decision should reject or retry rather than proceed. Cache
identity is the pair (`graph_instance_id`, `graph_generation`), and both are
null exactly when there is nothing to key on.

Persisted artifacts use strict bracketing and fail instead, because a
potentially mislabelled replay artifact is worse than no artifact.

`reset` (amended 2026-11, #271). Reset deletes in batches, and a bracket
cannot see it: the stamp stays still between batches, and once a batch
deleted the singleton, two reads of the missing node also agreed. So reset
keeps the singleton and writes a second `GraphMeta` row, `id: 'reset'`, in
its first transaction, removing it in its last, which also gives the
singleton a new instance id and generation 0. A bracket that reads the marker
before or after its query returns the unverified form at once, without
retrying, and a strict bracket raises `GraphResetInProgressError`. An
interrupted reset leaves the marker; running reset again finishes it. The
marker is a row, not a column, because a new column on Ladybug's typed
`GraphMeta` table would need a migration. It orders one reset against its
readers only: two overlapping resets are concurrent writers, out of scope as
a crawl during a reset is.

## Consequences

- Cache invalidation is a comparison of the pair (`graph_instance_id`,
  `graph_generation`), and only after checking `graph_state_verified: true` —
  an integer alone collides across a replaced graph, and an unverified response
  belongs to no state at all. Telemetry rows pin the pair for replay.
- Crawl of N servers = N increments (each server load is its own
  transaction) — acceptable: generation is an invalidation token, not a
  semantic version.
- Adds one bookkeeping node, which `reset` keeps and re-stamps with a new
  instance id and generation 0, plus a marker row that exists only while a
  reset runs (or after one was interrupted).

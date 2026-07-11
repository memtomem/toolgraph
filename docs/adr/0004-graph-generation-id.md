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

A single `GraphMeta` node carries `generation: int` (and `updated_at`).
Every successful `crawl` load and every successful `ingest-manifest`
increments it in the same transaction as the mutation. Query results that
feed the selector surface (and the MCP server responses) include
`graph_generation`. Failed/rejected operations do not increment.

Because Neo4j reads are read-committed, producers bracket a query with two
generation reads and retry when they differ. Live MCP queries retain the
historical availability-first fallback after bounded retry exhaustion: they
return the last query result stamped with the freshest generation so caches
self-heal on their next read. Persisted artifacts use strict bracketing and
fail instead, because a potentially mislabelled replay artifact is worse than
no artifact.

## Consequences

- Cache invalidation is one integer comparison; telemetry rows pin
  `graph_generation` for replay.
- Crawl of N servers = N increments (each server load is its own
  transaction) — acceptable: generation is an invalidation token, not a
  semantic version.
- Adds one bookkeeping node; `reset` clears it like everything else.

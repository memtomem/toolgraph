# 0009 — Trace feedback is immutable evidence with separate human annotations

Status: Accepted · Date: 2026-07-12

## Context

Tracegraph T3 exports a versioned, body-free `tracegraph.review-candidates`
report after matching repeated failure patterns. Its merged producer golden is
derived from the exact normalized artifact analyzed (Tracegraph PR #10).
SyncMill can already project the same exact tuple to an idempotent,
human-required board item (SyncMill PR #57).

Toolgraph G3 still needs a policy-review surface, but importing observations
into Neo4j would mix analysis evidence with authored governance truth. Editing
the Tracegraph report would destroy the producer artifact, and automatically
changing a manifest or selector would violate the human-review-only P4
boundary.

## Decision

1. `review-candidates list` strictly validates Tracegraph schema v1, accepts
   additive producer fields, and retains only `run_id`, `pattern_id`,
   `pattern_version`, qualified `tool_key`, and `artifact_digest`.
2. The candidate id is the UUIDv5 of the same canonical exact-tuple string used
   by SyncMill PR #57. The shared id enables correlation, not shared state.
3. The source report is immutable. `annotate` writes a separate
   `toolgraph.review-annotations` v1 sidecar bound to the SHA-256 of the exact
   report bytes. Events form a contiguous, append-only disposition chain.
4. Dispositions are `open`, `accepted`, and `dismissed`. Every event requires
   a reviewer and may include one bounded local note. Notes may cite paths but
   credential-shaped content is rejected. Any disposition may be corrected or
   reopened by a later event; the last event is current.
5. The sidecar writer serializes concurrent writers and uses temp-file,
   file plus parent-directory `fsync` (where supported), and atomic replacement.
   It resolves the sidecar target before loading, locking, or replacing so a
   symlink alias cannot fork history. A stale source binding, broken event
   chain, unknown candidate, or failed write preserves both source and prior
   sidecar.
6. These commands are artifact-only. They do not connect to Neo4j, alter a
   manifest or policy, affect preflight/selector behavior, or increment
   `graph_generation`. `accepted` authorizes no automatic apply path.

## Consequences

- G3 remains usable without Tracegraph or SyncMill installed and without a
  running Neo4j service.
- Toolgraph and SyncMill can independently review the same evidence while
  correlating it by candidate id; operators must not infer state synchronization.
- A later manual governance change must be a separate, explicit workflow with
  its own before/after blast-radius and regression evidence.
- The v1 sidecar is a closed Toolgraph-owned contract. Producer additive fields
  cannot be smuggled into annotation storage.

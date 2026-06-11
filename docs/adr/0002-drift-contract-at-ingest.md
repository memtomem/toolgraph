# 0002 — Drift contract: ingest notices for non-live tool refs + `--strict-drift`

Status: Accepted · Date: 2026-06-11

## Context

When a server stops exposing a tool, the loader keeps the Tool node if it
carries authored governance (deleting it would silently turn a DENY into an
ALLOW) and prints "re-run ingest-manifest to reconcile". But ingest resolution
(`resolve_tool_keys`) checks only Tool-node existence, not a live EXPOSES
edge — so re-ingesting the *same* manifest silently re-grants against the
stale node and the drift persists forever. The `drift` query can find these,
but nothing in the authoring loop ever points at it: the contract is passive.

Ingest warnings today mean "reject the whole manifest" (atomicity guarantee).
Drifted refs must not be forced into that channel: a fleet with one
temporarily-down server would become un-ingestable.

## Decision

1. Ingest gains a second, **non-fatal** output channel: *notices*. Phase 1
   emits a notice for every resolved tool ref that has no live EXPOSES edge.
   Warnings still reject; notices never do.
2. `ingest-manifest --strict-drift` promotes drift notices to warnings
   (reject), for CI pipelines that treat drift as an error.
3. The loader's reconciliation warning is reworded to state the actual fix:
   *edit the manifest to drop the tool (or re-crawl the server), then
   re-ingest* — not just "re-run ingest-manifest".

## Consequences

- `ingest_governance` returns warnings *and* notices; CLI and MCP surfaces
  print/return them distinctly. Callers of the old single-list shape must
  update (internal-only today).
- Drift becomes visible at the moment an operator touches governance, not
  only when someone remembers to run `drift`.
- A deliberately-kept drifted grant stays possible (default is notice, not
  reject) — operators in transition aren't blocked.

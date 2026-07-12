# 0008 — SyncMill enforcement consumes advisory Toolgraph evidence

Status: Accepted · Date: 2026-07-12

## Context

SyncMill P3.1 adds an opt-in pre-dispatch gate. Toolgraph is not in the runtime
traffic path and cannot truthfully claim that it stopped execution.

## Decision

Toolgraph `preflight` remains an advisory fact producer: artifacts retain
`mode: "advisory"`, policy findings retain the existing decision vocabulary, and
the CLI continues to exit 0 for allow, warn, and unresolved identity. SyncMill
validates those artifacts, applies its unknown/drift/availability policy, and
records the separate enforcement decision.

Focused contract fixtures pin unknown-tool and drift rows so a consumer can
apply different policies without parsing prose. The seven selector reject
reason literals remain the public compatibility boundary from ADR-0005.

## Consequences

Toolgraph can be used without SyncMill and remains advisory by default. A future
producer-side enforcement or structured failure artifact requires a separate,
versioned contract change.

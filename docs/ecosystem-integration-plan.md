# toolgraph / tracegraph / syncmill integration plan

**Status:** first integration milestone, P3.1 preview, T3, SyncMill board import
and Toolgraph G3 complete (2026-07-12)
**Written:** 2026-07-11
**Canonical source:** the full plan, implementation design and smoke runbook live
under `docs/ecosystem/` in the private companion repositories. This document
covers only the part toolgraph owns and is kept readable on its own, without
following those links.

> The canonical source distinguishes the order things actually landed from the
> design labels. P0/P1 and the first milestone, P2 shared CLI/dashboard
> rendering, and the real identity / qualified-tool advisory P3 and Gate D are
> complete. Strict P3.1 consumer enforcement shipped as a SyncMill opt-in
> preview, and Gate E operational-evidence approval is still open. The Tracegraph
> T3 producer, the SyncMill board import slice and Toolgraph G3 artifact review
> are also complete, but live qualified-tool telemetry and operational review
> evaluation without automatic application remain in the wider P4 follow-up
> scope. In the stage 0 and stage 1 checklists, the only open item is the
> cross-repository one: having the SyncMill run report record the preflight
> `artifact_digest`.

## Summary

The three projects are not competitors; each covers a different point in the
agent execution lifecycle.

| Project | Role | When |
| --- | --- | --- |
| toolgraph | MCP tool access, policy and impact analysis | Before execution |
| syncmill | Isolated execution of several coding agents, and reconciling results | During execution |
| tracegraph | Causal analysis of execution history and regression detection | After execution |

Toolgraph's recommended position is an advisory preflight that inspects the
usable tool candidates before syncmill distributes work. Failure signals
observed in tracegraph can inform later policy and tool-selection improvements,
but must never change an ALLOW/DENY policy automatically.

## Current baseline

- Crawls MCP servers to build `Agent -> Tool -> Resource -> Policy` paths.
- Provides `check-access`, `unsafe-tools`, `blast-radius` and the negative-truth
  audits.
- Verdicts are explainable results carrying provenance — not runtime
  enforcement.
- The selector surface is limited to deterministic filtering and feature
  derivation.

## Goals

1. Inspect the policy risk and drift of the tool set before a syncmill run.
2. Persist the basis for each verdict as a per-run artifact, linkable to the
   execution result and its trace.
3. Feed failure patterns found in tracegraph back as policy review candidates.
4. Do **not** grow toolgraph into a runtime gateway or a learning system.

## Non-goals

- toolgraph mediating or blocking agent subprocess execution directly
- Storing tracegraph's raw spans or syncmill's full prompts in Neo4j
- Automatically editing the governance manifest on the strength of a single
  observed failure
- Merging the three repositories into one package or database

## Proposed interface

The implementation is a CLI command wrapping the existing selector surface
(`eligible_tools` / `rank_features`, ADR-0005). The MCP `_with_generation`
bracketing is extracted into `graph/queries.py` so the CLI can share it.

```
toolgraph preflight AGENT [CANDIDATES...] --profile strict|review|explore
                    --run-id RUN_ID [--features] [--out preflight.json]
```

### Preflight request

```json
{
  "schema_version": 1,
  "run_id": "stable-run-id",
  "agent": "codex",
  "candidate_tools": ["filesystem::read_file"],
  "policy_profile": "review"
}
```

### Preflight result (`contracts/preflight.schema.json`)

```json
{
  "schema_version": 1,
  "kind": "toolgraph.preflight",
  "run_id": "stable-run-id",
  "created_at": "2026-07-11T00:00:00+00:00",
  "graph_generation": 42,
  "agent": "codex",
  "agent_found": true,
  "profile": "review",
  "mode": "advisory",
  "eligible": ["filesystem::read_file"],
  "rejected": [{"candidate": "…", "tool_key": "…", "reason": "DENY_GOVERNED", "paths": ["…"]}],
  "features": [],
  "decision": "advisory_allow | advisory_warn | unresolved_identity"
}
```

`schema_version` is an **integer** (shared across the ecosystem — it matches the
tracegraph artifact contract). `decision` is derived as follows: `agent_found`
false → `unresolved_identity`; any rejected rows → `advisory_warn`; otherwise
`advisory_allow`. The exit code is 0 even for `advisory_warn` — advisory is not
enforcement, preserving the opt-in gating contract of ADR-0003.

The contract carries `graph_generation`, qualified tool keys, verdicts,
classification and provenance. It does **not** carry prompts, credentials or
sensitive resource payloads. A **redaction pass** (stripping userinfo and query
from resource URIs) is applied to `paths` and provenance strings — this command
owns the risk that provenance evidence could carry a credential. Redaction
fixtures are part of the contract tests.

## Staged plan

### Stage 0: freeze the contract

- [x] Document qualified tool keys and agent identity mapping — the
  server-qualified key and the `agent_found` / `unresolved_identity` contract
  are captured in `contracts/preflight.schema.json` and the guide documents.
- [x] Define the preflight JSON Schema and its version policy —
  `contracts/preflight.schema.json` (including the additive-within-major
  policy), pinned by `tests/test_preflight_contract.py`.
- [x] Decide consumer policy per `DENY`, `NOT_GRANTED`, drift and unmapped state
  — fixed by ADR-0008 and the drift / unknown-tool / unresolved-identity
  fixtures under `contracts/fixtures/`.
- [x] Confirm the scope given that syncmill does not use MCP tools directly
  today — ADR-0008 records the advisory-producer / SyncMill-enforcement
  boundary.

### Stage 1: manual artifact integration

- [x] Add an example that exports the preflight result as a JSON artifact using
  the existing CLI — the `toolgraph preflight … --out preflight.json` example in
  the README.
- [x] Record `run_id` and `graph_generation` in the artifact — both are required
  by the schema and written by `toolgraph/preflight.py`.
- [x] Add the provenance/path redaction pass and its fixtures — `redact()` in
  `toolgraph/preflight.py`, pinned by the planted-violation scanner in
  `tests/test_preflight_contract.py`.
- [ ] Have the syncmill run report reference that artifact path or digest — the
  producer side is done: with `--out`, `preflight` prints an `artifact_digest`
  envelope over the file bytes to stdout. Having the SyncMill run report record
  that digest remains a cross-repository task.
- [x] Add contract tests so a stale graph or an unknown agent is never mistaken
  for success — `tests/test_preflight.py`, `tests/test_preflight_graph.py` and
  `tests/test_preflight_contract.py` pin `unresolved_identity` and generation
  bracketing.

### Stage 2: optional syncmill adapter

- [x] Jointly define syncmill's opt-in preflight enforcement requirements.
- [x] Keep the Toolgraph producer advisory; the SyncMill mode enforces the
  chosen profile.
- [x] Specify unknown/drift/unavailable policy and the fail-open / fail-closed
  override.
- [x] The guarantee that a strict block happens before agent/worktree execution
  is verified by tests in the companion PR on the SyncMill side, and completed
  when that PR merged.

### Stage 3: trace feedback

- [x] Import the producer-derived v1 pattern results from the Tracegraph
  companion PR as review candidates.
- [x] Project only `run_id`, qualified tool key, pattern id/version and artifact
  digest.
- [x] Leave the source report immutable and record operator disposition history
  in a separate sidecar.
- [x] Cross-reference candidates and board items by the same exact-tuple UUIDv5
  as the SyncMill companion PR.
- [x] Pin by regression test that even `accepted` modifies no manifest, policy,
  selector or Neo4j state, and that blast radius and `graph_generation` are
  preserved.
- [x] Export accepted candidates as a `policy review-plan` artifact that binds
  them to the current agent/profile verdict and graph state, fixed to
  `human_required` and `automatic_change: false`. Actual manifest changes,
  ingest and bundle compilation stay explicitly separate.

G3 annotations and SyncMill board state are independent. They can only be
correlated by candidate id and are never synchronised automatically. If an
actual manifest change is needed later, it must happen in a separate explicit
workflow that records before/after blast radius and regression evidence.

## Verification criteria

- The same graph generation and inputs produce the same preflight result.
- Every rejection reason in a result is explainable through a graph path and
  provenance.
- Toolgraph's existing CLI and MCP API keep working with neither trace nor
  syncmill present.
- With the integration features off, current performance and the exit-code
  contract are unchanged.
- No secret-bearing input is recorded in artifacts, logs or provenance.

## Key risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Advisory results mistaken for enforcement | State the decision mode in the artifact and the UI |
| The graph is older than the moment of execution | Verify `graph_generation` and the crawl timestamp |
| Agent/tool identifier mismatch | Use server-qualified keys and explicit mapping |
| A trace failure auto-promoted into a wrong policy | Keep feedback review-only, with operator approval |
| Tight coupling between analysis layers | Share only JSON artifacts and optional adapters |

## Definition of done

The first integration milestone counts as complete once stages 0 and 1 are
finished and a single real run can be traced through the preflight artifact, the
syncmill run result and the tracegraph artifact under the same `run_id`.
Automatic policy changes and a runtime gateway are not completion conditions for
this plan.

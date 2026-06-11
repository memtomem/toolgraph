# 0005 — Selector surface: deterministic filter/features in, learning out

Status: Accepted · Date: 2026-06-11

## Context

toolgraph's governance graph can serve tool selection as a context-engineering
layer: shrink the candidate catalog deterministically before a relevance
ranker sees it (see `docs/context-engineering-tool-selection-report.md`).
The temptation is to keep going — relevance scoring, learned ranking,
reinforcement learning — inside toolgraph. External review (2026-06-11) was
blunt: RL now is theater (no reward signal, no trajectories, no evaluation
loop), and the selector API is sound only while it stays deterministic.

## Decision

toolgraph exposes a **deterministic** selector surface and nothing more:

- `eligible_tools(agent, candidates, profile)` — hard filter with reject
  reasons and policy-evidence paths. Profiles are named rule sets
  (`strict` / `review` / `explore`), not learned thresholds; production
  default is `strict`.
- `rank_features(agent, candidates)` — batch per-candidate facts (resolution,
  grant, DENY paths + classification, drift, unmapped, unbacked) plus a
  **rule-based** `risk_score` from a published fixed table.
- `selection_explain(agent, tool)` — compact human-readable reasons, a view
  over `check_access` + audit facts.

Out of scope, permanently delegated to the consumer (harness): relevance /
task-match scoring, learning-to-rank, contextual bandits, any RL, prompt
optimization. If learning ever happens it consumes toolgraph's candidate
sets from outside. A learned model may never override a hard reject
(`NOT_GRANTED`, `violation`, drifted).

## Consequences

- Every selector answer stays explainable as a graph path — the product
  thesis extends to selection instead of diluting into ML infra.
- Build order is gated: telemetry schema and offline eval are design-doc-only
  until a real consumer integrates `eligible_tools` (acceptance: the
  agent-harness demo passes the report's regression criteria).
- Latency work (batching, `graph_generation` caching — ADR-0004) belongs to
  this surface; model serving never will.

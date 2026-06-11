# Architecture Decision Records

Decisions that shape toolgraph's contracts. Each ADR is small: context,
decision, consequences. Statuses: Proposed / Accepted / Superseded.

| ADR | Title | Status |
|-----|-------|--------|
| [0001](0001-node-existence-is-a-truth-signal.md) | Node existence is a truth signal — prune every authored node type | Accepted |
| [0002](0002-drift-contract-at-ingest.md) | Drift contract: ingest notices for non-live tool refs + `--strict-drift` | Accepted |
| [0003](0003-cli-output-and-exit-code-contract.md) | CLI contract: stdout is machine output, exit codes are opt-in gates | Accepted |
| [0004](0004-graph-generation-id.md) | `graph_generation`: a monotonic id stamped at crawl/ingest | Accepted |
| [0005](0005-selector-surface-boundary.md) | Selector surface: deterministic filter/features in, learning out | Accepted |
| [0006](0006-crawl-tool-annotations-as-self-claims.md) | Crawl tool annotations as self-claims (`confidence: medium`) | Accepted |
| [0007](0007-fleet-level-crawl-reconciliation.md) | Fleet-level crawl reconciliation: servers.yaml declares the fleet | Accepted |

Background for 0003–0005: `docs/context-engineering-tool-selection-report.md`
and the 2026-06-11 external (Codex) review of the improvement plan.

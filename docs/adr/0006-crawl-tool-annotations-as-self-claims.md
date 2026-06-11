# 0006 — Crawl tool annotations as self-claims (`confidence: medium`)

Status: Accepted · Date: 2026-06-11

## Context

MCP tools may carry annotations (`readOnlyHint`, `destructiveHint`,
`idempotentHint`, `openWorldHint`). The crawler ignores them today. They are
crawlable facts that directly feed governance review — but they are the
*server's own claims about itself*, unverified by anyone. The provenance
model already distinguishes claim strength; annotations must not launder
into the same confidence tier as protocol-level facts.

## Decision

1. The crawler stores annotations as Tool-node properties on the same
   crawl pass that owns the node.
2. Provenance: `source: crawled`, **`confidence: medium`** — a hint is a
   self-claim, not a verified fact. (EXPOSES/PROVIDES stay `high`: "the
   server advertised this tool" is protocol-observable; "this tool is
   read-only" is not.)
3. Two new negative-truth queries join annotations against authored truth:
   - `destructive-unsafeguarded` — tools hinting destructive with no
     GOVERNED_BY (direct or via any touched resource).
   - `annotation-contradictions` — authored WRITES edges on tools hinting
     `readOnlyHint: true` (one side is wrong; both cite their provenance).
4. Annotations never auto-create READS/WRITES/GOVERNED_BY edges. They
   surface audit rows; the operator authors the edge.

## Consequences

- Authoring gets a priority queue from crawled data at zero authoring cost.
- A lying server can still lie — but the contradiction query makes the lie
  *visible* the moment an operator asserts otherwise, which is the
  analyzer's job; enforcement remains out of scope.
- Tool-node property surface grows; loader reconciliation must clear
  annotations the server no longer sends (same lifecycle as description).

# Stabilization and integration evidence — 2026-09-08

## Scope and prior state

Baseline: `b34cec43cedb4f375125136ba731b87646094cb7`, PR #92 stacked on
PR #91. Reviewed all 31 tracked Markdown documents (3,801 lines), the parent
Cowork handoff/roadmaps, memtomem search results and Codex history. No missing
inline relative Markdown link targets were found. Historical private-docs copies
are backups, not relocated executable entrypoints.

Live GitHub readback found 496 tests and `ci-required` passing on that baseline
(run `34198929578`). Its optional dev audit reported pip 26.1.2; the new lock
resolves pip 26.2.1, and the all-groups/all-extras dev audit now passes locally.
PR #92's old body still described 408 tests and an account-wide CI block.
The older main gateway run `34100875676` passed, but does not test this change.

## Changes

- Shared, bounded reference resolution for ingest, selector and single-tool
  queries: exact qualified keys and all ambiguous bare-name matches preserved.
  Ingest validation order, last-entry-wins and its whole-manifest transaction
  remain unchanged; the resolution map is transaction-local, not a stale cache.
- Ecosystem smoke now requires three exact local commit SHAs, clones those
  objects including unpushed commits, and uses the selected governance fixture.
  No main-branch fallback or worktree overlay. Git routing environment variables
  are stripped for disposable Git operations. Package SHAs and runner digest are
  separate evidence: changing the harness does not relabel tested packages.
- Gateway smoke requires clean HEADs matching both supplied SHAs, checked before
  process launch and before success. Both runners include installed dependency
  lists and lockfile digests; ecosystem output also binds retained artifacts.
- `make install` includes Ladybug. CI/make gateway invocations pass the resolved
  SHAs. Current docs separate shipped consumer features, historical operating
  evidence, and current-head verification.

Public CLI/MCP decision shapes, schema v1, reason vocabulary and artifact digest
formats are unchanged. The internal smoke CLI now requires explicit SHA flags.

## Local verification

- Full pytest: **511 passed, 3 existing warnings**, 54.49 seconds, real isolated
  Neo4j 5.26 and Ladybug. Includes 15 new parameterized cases for batch limits,
  repeated refs, diagnostic order, rollback, dirty overlays, incorrect SHAs and
  inherited Git routing, health counter parsing and dependency-path exclusion.
- Ruff check: passed. Dev dependency audit (all groups and extras): no known
  vulnerabilities. Wheel/sdist build and Twine checks: passed.
- Both integration runners passed against code commit
  `ef298d5b3c4a2129af3a3d4125d1f8a8541e091f`; see pinned evidence below.
  Remote CI is checked separately on the final pushed documentation commit.

## Ingest benchmark

`python scripts/benchmark_ingest.py --baseline-ref <full-sha> --output <json>`
uses disposable databases, loads the baseline query and ingest implementations
from Git, and runs one warmup plus three measured ingests per variant and size.
Each fixture has one agent, one ALLOW policy, one grant per tool and 100 repeated
grants. Crawl/schema creation are outside the timing; the full write transaction
is inside it. Baseline always runs first, so these are illustrative measurements,
not a controlled general comparison between engines or an SLA.

| Backend | Tools | Queries before → after | Median ms before → after | Python peak bytes before → after |
| --- | ---: | ---: | ---: | ---: |
| ladybug | 100 | 212 → 13 | 48.30 → 16.86 | 76,850 → 85,449 |
| ladybug | 1,000 | 1,112 → 13 | 267.21 → 83.17 | 592,903 → 709,671 |
| ladybug | 4,097 | 4,209 → 14 | 1254.08 → 557.56 | 2,573,806 → 3,132,431 |
| ladybug | 10,000 | 10,112 → 15 | 4066.81 → 2640.70 | 6,888,796 → 8,178,082 |
| neo4j | 100 | 212 → 13 | 214.27 → 32.65 | 1,317,839 → 201,258 |
| neo4j | 1,000 | 1,112 → 13 | 751.65 → 137.41 | 7,318,613 → 1,057,076 |
| neo4j | 4,097 | 4,209 → 14 | 2647.63 → 528.46 | 27,995,720 → 4,327,447 |
| neo4j | 10,000 | 10,112 → 15 | 6090.93 → 1327.29 | 67,793,801 → 11,165,362 |

Memory uses tracemalloc and excludes native engine, container and process RSS.
The retained resolution map increases Ladybug's Python allocation peak; the
optimization is not a universal memory reduction. At 10,000 tools, both engines
reduce 10,112 queries to 15 while preserving one atomic transaction.
Raw samples were captured in `/private/tmp/toolgraph-stabilization-benchmark.json`;
the table above retains the durable evidence if that temporary file disappears.

## Remaining boundaries

No paid Codex/Claude/Kimi provider canary or persistent strict rollout is part
of this milestone. Gate E/P4's July success and explicit per-project approval
remain historical evidence. STM/SyncMill enforce; Toolgraph remains advisory.

Issue #89's observation binding, role-specific consumer routing, MCP 2.x migration
and public publication are separate follow-ups. Observations must be bound to
policy/graph/profile/time-window context before broadening grant proposals.
Private-to-public visibility and PyPI publication are not implied by these fixes.

## Pinned integration evidence

Both summaries report `status: pass`. Real MCP/consumer processes ran in isolated
state directories; provider execution used fixtures, with no paid model canary.
Dependency evidence retains only package names/versions and lock digests.

| Input | Commit |
| --- | --- |
| Toolgraph | `ef298d5b3c4a2129af3a3d4125d1f8a8541e091f` |
| memtomem-stm | `b0dad89e9fb3799069a5b44330668504ce5afe0b` |
| SyncMill | `96c36d98c1d2922640e3c144eb8cd9914bc6ad5a` |
| Tracegraph | `d048f400fdd39bb37c9608e45ee5931cba058b46` |

Gateway verified filtered discovery, warm-cache denial before upstream execution,
restart filtering, contract drift, missing/corrupt strict startup failure, review
visibility and call-through with actual health counter **1**, and disabled-mode
call-through despite a missing bundle. STM did not import Toolgraph.

Ecosystem route returned `advisory_allow`; compete returned `advisory_warn` with
`syncmill::board_stats:DENY_VIOLATION` and the expected fixture winner. Retained
preflight SHA-256 digests:

- route: `5ba16d9e5411a495e9a571883b7aa4f225c4d85e8096c60aba2bd540e01f93ea`
- compete: `21f5a66c4ffa6475ff20e2c2d28c205b3dfbcea686f215abac44cef157109433`
- harness: `001b2c8269149c5868ea437dcebbc5bfe01247dd23bae889e4475ea210a50225`
- governance: `d14c401ad3fc980f3723a40957c2ead6745d9fab9cc36c91b6ea49f2871a2d48`

Full local summaries are in `/private/tmp/toolgraph-stabilization-gateway-final`
and `/private/tmp/toolgraph-stabilization-ecosystem-final`. Later documentation
commits do not change these tested source objects.

## Delivery sequence

PR #91 was merged after its required CI and CLA passed on the bound head. Its
optional dev audit still reported the older pip; this PR's lock update resolves
that finding. PR #92 is retargeted to main for final remote validation. Neither
public visibility nor package publication is part of this delivery.

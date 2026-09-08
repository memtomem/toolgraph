# Toolgraph implementation review

Historical review dated 2026-09-07. [Korean companion](ko-2026-09-07-implementation-review.md).
This English edition preserves the findings and measured evidence; the paired
Korean record retains the expanded reproduction narrative and original snippets.
Subsequent fixes are tracked in [remediation verification](2026-09-07-remediation-verification.md)
and [stabilization](2026-09-08-stabilization.md). These are not unresolved findings
against the release candidate.

## Baseline and method

Reviewed `45b594872cf0d94635aa26db8a7f6fe6464159ae` on `perf/p1-p2-backlog`,
starting clean. Scope covered crawler, loader, manifest ingest, selectors,
CLI/MCP, preflight/bundle/review artifacts, adapters, contracts, CI and packaging.
The core Python package contained approximately 6,339 lines excluding quickstart.
Host: macOS 26.6.2 arm64, Python 3.12.11. Test dependencies: Ladybug 0.18.3,
Neo4j driver 6.2.0, MCP 1.28.1, Pydantic 2.13.4, pytest 9.1.1, Ruff 0.15.22,
jsonschema 4.26.0. Neo4j used an isolated `neo4j:5.26-community` testcontainer;
Ladybug used temporary databases. No user database was touched.

The baseline suite passed 414 tests, but additional executable probes reproduced
two P1, six P2 and one P3 finding. P1 affects default use or core trust; P2 affects
specific inputs/scales/integrations; P3 is a narrower consistency issue.

## Architecture and strengths

Toolgraph produces advisory governance decisions and evidence; consumers enforce.
It has no scheduler/executor. Accepted review candidates do not automatically
change manifests or graphs. Unknown, ambiguous, ungranted, DENY, exception, drift
and unmapped cases remain distinct. Authored replacement and graph generation
share a transaction; rejected ingest preserves state. Successful reset creates a
new instance ID at generation zero. Crawled facts, operator assertions and
annotation self-claims remain distinct. Selector queries are batched; additional
Ladybug audit probes preserved cross-agent exception isolation. Artifact writes
use private atomic replacement and fsync; publication authority is isolated from
build code. These strengths do not prove production readiness.

## Reproduced findings

| ID | Severity | Finding and reproduction | Required correction and regression |
| --- | --- | --- | --- |
| R1 | P1 | Ladybug cannot infer struct members for empty UNWIND lists. Tools/resources counts 1/0, 0/1, 0/0 fail; 1/1 passes. `Governance(agents=['a'])` without policies also fails. Neo4j accepts all five. Error: `r has data type STRING but (NODE,REL,STRUCT,ANY) was expected`. | Skip only empty upserts, not stale EXPOSES/PROVIDES removal, revocation or generation updates. Test nonempty-to-empty, empty manifests and rollback on both backends. |
| R2 | P1 | Recursive artifact redaction collapses `https://agents.example/worker?tenant=A` and `?tenant=B` to the same principal, although only A has `s::t`. Both backends output the same ID with contradictory eligible/rejected results. URI run IDs also lose query data. External authorization bypass was not established. | Preserve non-sensitive identities exactly; reject unacceptable identities before I/O. Redact evidence fields only. Test distinct query/fragment IDs and serialized round trips. |
| R3 | P2 | `policy_review.current_paths` exports a synthetic resource URI's userinfo and token query, while bundle paths scrub them. Both backends reproduce this using an accepted review sidecar and a DENY resource. This is missing export defense, not arbitrary file access. | Redact review evidence paths, not the whole artifact. Verify saved CLI JSON and preserve candidate/run/source digests. File mode 0600 alone does not sanitize shared content. |
| R4 | P2 | Concatenating `agent/tool/resource/policy` with a pipe collides for resource `urn:x\|y`, policy `p` versus resource `urn:x`, policy `y\|p`. Two exceptions become one and one path falsely violates; engines retain different rows. False allow was not demonstrated. | Use a typed canonical tuple digest; define duplicate logical exception order. Test delimiters, wildcard/specific cases, counts and classifications on both backends; check re-ingest compatibility. |
| R5 | P2 | Loader accepts five schema-invalid mutations: missing maximum_cycles, numeric string maximum_agent_calls, boolean schema_version, string agent_call, null timeout_seconds on a non-agent node. | Align required fields, strict types and field presence with the published schema. Differential tests must retain additive same-major fields and reject unsupported majors. Topology findings remain separate from invalid input. |
| R6 | P2 | A catalog of 4,097 exposed tools fails compilation with `4097 candidates exceed the 4096 limit`, even with no grants. Both engines reproduce this. | Keep the public selector bound; evaluate compiler chunks under one strict graph-state bracket, retrying the entire attempt on drift. Test 4,095/4,096/4,097, multiple chunks, coverage and mid-chunk changes. |
| R7 | P2 | Import-time Settings reads malformed default JSON before CLI help/version or explicit valid config can run. Fresh subprocesses exit 1 with a traceback and empty stdout. | Defer settings I/O until configuration is selected. Test malformed cwd/environment defaults, valid explicit config, independent help/version and concise invalid-explicit errors. |
| R8 | P2 | Injected reader B supplies state `injected-B`, generation 99 and governance digest of 64 `b` characters, but the bundle attaches runtime Ladybug A's instance/generation 0. Default RuntimeGraphStore does not exhibit the source mismatch. | Delegate every bracket state read to the injected reader. Test independent readers, drift retries/failure and no runtime DB access. |
| R9 | P3 | Custom naive created_at produces timezone-free output in preflight/review-plan, unlike bundle/control-preflight. The installed JSON Schema format checker lacks date-time, masking the error. Default UTC CLI clocks are unaffected. | Apply common RFC3339 offset validation before I/O and install/assert the actual date-time checker. Test naive, undefined offsets, seconds offsets and valid UTC/minute offsets. |

## Performance observations

One agent, one ALLOW policy and one grant per tool; real DB session wrappers
counted calls without mocking query results. Each size was measured once.
Crawl/schema setup was outside timing; the ingest write transaction and rank
operation were timed. Every rank result had N rows.

| Backend | Tools | Ingest queries | Ingest ms | Rank queries | Rank ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Ladybug | 10 | 22 | 14.76 | 5 | 8.46 |
| Ladybug | 100 | 112 | 32.83 | 5 | 8.56 |
| Ladybug | 500 | 512 | 111.92 | 5 | 12.26 |
| Neo4j | 10 | 22 | 68.42 | 5 | 194.73 |
| Neo4j | 100 | 112 | 143.23 | 5 | 158.73 |
| Neo4j | 500 | 512 | 409.30 | 5 | 164.42 |

Ingest remained N+12 because Phase 1 resolved each reference separately despite
batched Phase 2 writes. The next optimization was resolving repeated references
within the same transaction, not splitting atomic replacement. These mixed
cold/warm samples include Docker/Bolt/planning overhead for Neo4j and are neither
an SLA nor a general engine comparison. Sustained load, concurrent writers,
remote latency and peak RSS were not measured.

## Deliberate constraints

Keep whole-manifest transactions, Ladybug single ownership and Neo4j for shared
service use. Batched reset needs a maintenance window and may require rerunning
after interruption; a successful reset probe does not establish concurrency
safety. ADR-0004 live-read exhaustion fallback is weaker than persisted artifacts'
strict bracket. Empty manifests/fleets retain ADR-0007 deletion semantics.
Do not delete persistent sidecar lock files: their retention avoids unlink races.
Do not add a runtime executor or automatic policy mutation to resolve these bugs.

## Verification and limits

- PASS: 414 tests, three warnings, 36.22 seconds, no skips; Ruff and unchanged
  lock check (88 packages); two-backend rollback and successful reset checks.
- PASS: wheel/sdist, Twine, allowlist (48 entries), same-environment wheel rebuild,
  runtime/extras vulnerability audits and standalone quickstart.
- Standalone installation selected MCP 1.29.1, Neo4j driver 6.3.0, Pydantic 2.13.5,
  Ladybug 0.18.3. It stored one server, two tools and one resource; the bundle
  contained one eligible and one rejected tool, generation 2, mode 0600. The
  full suite was not repeated against that independently resolved environment.
- Same-environment wheel SHA-256:
  `31bb4ec52e68801049ba8998f5e05c3245c65e4a0b2accd26e0b4be524e6324a`.
- Initial sandbox Docker/cache failures were rerun with access; the final passing
  result supersedes setup errors. Warnings were two deprecated testcontainers
  wait APIs and a pydantic-settings lifespan forward reference.
- NOT RUN: actual consumer end-to-end enforcement, remote CI/settings/PyPI,
  other platform/Python combinations at this source snapshot, customer-scale
  stress/crash/power-loss tests, history secret scan and production auth review.

Fix order: R1, R2/R3, R4/R5, R6/R8, R7/R9, then ingest reference batching.
Each fix needs a regression for its reproduction and the full existing suite.
Local baseline passes alone are not release approval.

The Korean companion retains historical temporary evidence locations, source
line references and reproduction snippets. Temporary probes are supplementary;
substantive inputs, errors and measurements are preserved in these paired files.

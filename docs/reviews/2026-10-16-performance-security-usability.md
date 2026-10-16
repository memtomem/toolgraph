# Performance, security, and usability review — 2026-10-16

Baseline: `1d543b94ec62fbc3821f59a05870c1e042bf1586` (clean checkout at start).
Runtime inspected: Toolgraph 0.2.0, Python 3.12.11, MCP 2.3.0, Ladybug 0.18.3,
pytest 9.1.1; macOS 26.6.2 arm64. Findings below are scoped observations, not
a complete security audit or a production readiness verdict.

## Registered work

All five new issues are P2 follow-ups. No runtime fixes were made.

| Area | Issue | Evidence and requested outcome |
|---|---|---|
| Performance | [#151](https://github.com/memtomem/toolgraph/issues/151) | Both fleet duplicate checks repeatedly call `list.count`; replace quadratic scans with linear aggregation while preserving identity rules. |
| Usability | [#152](https://github.com/memtomem/toolgraph/issues/152) | `concurrency=0` waits indefinitely before entering the per-server timeout; reject invalid library arguments before task scheduling. |
| Performance / resource hardening | [#153](https://github.com/memtomem/toolgraph/issues/153) | Page count bounds do not bound retained catalog bytes or items; measure and define collection budgets without accepting truncated catalogs. |
| Operational usability | [#154](https://github.com/memtomem/toolgraph/issues/154) | Blank and comment-only files and `{}` request complete fleet retirement; require explicit `servers: []` for that intent. |
| Security documentation | [#155](https://github.com/memtomem/toolgraph/issues/155) | SECURITY.md still restricts fixes to `0.0.x`, while the source/changelog identify 0.2.0; clarify latest-published-release support. |

[ADR-0014](../adr/0014-explicit-empty-fleet-declaration.md) records the
explicit-empty-fleet proposal. It is **Proposed**, not implemented or
accepted. ADR-0007 remains Accepted. The issue independently contains the
behavioral proposal because the ADR is local and uncommitted.

The collection-budget finding is a hardening task, not a demonstrated remote
exploit. The synthetic probe did not attempt exhaustion. Application checks
after SDK decoding cannot establish a peak transport/parse memory bound;
issue #153 explicitly requires that distinction and measured budget choices.

## Measurements and reproductions

### Fleet duplicate checks

Measured both helpers in `toolgraph/crawler/crawl.py` using unique `s0` through
`sN` names. The identity helper received named synthetic failures. Construction
was outside the timed region; each cell is the median of five invocations.
No network or database was involved. These are local microbenchmarks.

| Servers | `duplicate_name_overrides` | `duplicate_identities` |
|---|---:|---:|
| 1,000 | 4.821 ms | 4.823 ms |
| 2,000 | 17.173 ms | 17.165 ms |
| 4,000 | 73.075 ms | 72.983 ms |
| 8,000 | 306.717 ms | 307.026 ms |

Issue #151 includes the reproduction code. The requested validation is a
repeatable size sweep, not a hardware-specific CI latency threshold.

### Concurrency and collection bounds

- A nonempty `crawl_all_async(..., concurrency=0)` invocation completed only
  through an outer `asyncio.wait_for(..., 0.1)` timeout. No subprocess started.
  The semaphore is acquired before the per-server timeout begins. The issue
  applies to library callers; the CLI uses the valid default of five.
- During initial review, a synthetic one-page tools session with a
  1,000,000-character description returned that description intact through
  `_list_all_tools`. Source inspection found the page cap but no accumulated
  byte/item cap. No claim is made about an exploit or measured peak RSS.

### Empty fleet configuration

Each case used its own temporary Ladybug database under `/private/tmp`,
closed before cleanup. A fixture seeded one server, two tools, one resource,
and authored grant/data-access/DENY governance on one tool. No operator
database or external MCP server was accessed.

| Configuration | CLI exit | Remaining servers / tools / EXPOSES | Generation delta | Governed verdict |
|---|---:|---|---:|---|
| Empty file | 0 | 0 / 1 / 0 | +1 | DENY |
| Comment-only file | 0 | 0 / 1 / 0 | +1 | DENY |
| `{}` | 0 | 0 / 1 / 0 | +1 | DENY |
| `servers: []` | 0 | 0 / 1 / 0 | +1 | DENY |
| `servres: []` (invalid-key control) | 1 | 1 / 2 / 2 | 0 | DENY |

Every case started with 1 server, 2 tools, and 2 EXPOSES edges. The governed
resource survived all cases. The invalid-key control preserved the complete
graph state token and counts. The first four results confirm ADR-0007's
current intentional behavior; they do not show an authorization bypass.
Refusal of blank files is future acceptance criteria, not a passing test of
the current code.

## Additional review and existing work

Inspected graph-state bracketing and batched resolution, manifest validation
and ingest transaction boundaries, policy-bundle completeness and provenance
checks, bounded control-plan evidence, artifact identity/evidence handling,
private atomic writes, annotation writer locking, and CLI/MCP error handling.
This pass did not establish another independently actionable new defect in
those inspected paths. It does not establish that they are defect-free.

Existing open/closed issues and open PRs were checked before registration;
no open PR was returned. These existing open issues were retained as their
own work items, without duplicate issues or unsolicited comments:

- [#129](https://github.com/memtomem/toolgraph/issues/129): a durability
  warning escalated to an error after publication can invite duplicate retry.
- [#130](https://github.com/memtomem/toolgraph/issues/130): credential-shape
  rejection differs between notes, identities, and control-plan strings.
- [#133](https://github.com/memtomem/toolgraph/issues/133): rejected Neo4j
  credentials can produce a raw CLI traceback.
- [#145](https://github.com/memtomem/toolgraph/issues/145): Neo4j notification
  noise on fresh or un-ingested graphs.
- [#146](https://github.com/memtomem/toolgraph/issues/146): duplicate
  Configuration check names in doctor output.
- [#147](https://github.com/memtomem/toolgraph/issues/147): sample environment
  file paths do not point at files present in the repository root.

There were six existing open issues; #149 was closed. Historical notes were
used only to orient review; current source and probes determine the
observations here. No existing issue was closed or claimed fixed.

## Validation and limits

Initial review:

```sh
TOOLGRAPH_TEST_NO_DOCKER=1 .venv/bin/python -m pytest -q tests/test_crawler.py -k 'list_all or servers_yaml'
```

Result: **6 passed, 7 deselected**, one testcontainers deprecation warning.

Additional boundary checks:

```sh
TOOLGRAPH_TEST_NO_DOCKER=1 .venv/bin/python -m pytest -q -m 'not neo4j' \
  tests/test_artifact_safety.py tests/test_server_errors.py \
  tests/test_policy_bundle.py tests/test_control_plan.py \
  tests/test_manifest_diff.py tests/test_graph_generation.py \
  tests/test_cli_backend_outage.py
```

Result: **166 passed, 13 deselected**, one testcontainers deprecation warning.
The separate temporary-Ladybug reproduction above also passed its assertions.

Not run: full suite, Neo4j integration, live upstream MCP servers, production
load/RSS measurement, fresh dependency vulnerability scan, package-index
publication verification, or post-fix acceptance tests. The new issues define
future work; passing existing tests does not validate proposed fixes.

The initial delivery consists of five public GitHub issues and local review/ADR Markdown
files. No runtime files were changed, and no commit, push, or PR was created.
All five issue titles and bodies were read back and matched their submitted
drafts exactly. Local Markdown link targets resolve and `git diff --check`
passed.

## Product differentiation and practical usability — additional review

### Positioning hypothesis

For platform/agent development teams operating several MCP servers, the most
promising product outcome is: **understand cross-server policy exposure,
preview the consequences of a change, and catch regressions on the next
change**. This is a product hypothesis based on source inspection and a
synthetic walkthrough, not customer research or a claim of market uniqueness.
This audience is the working default; no narrower customer segment was
confirmed during this review.

Discovery, inspection, and authorization already have established tools:

- The [official MCP Registry](https://modelcontextprotocol.io/registry/about)
  provides server discovery metadata. Discovery alone is not a strong reason
  to choose another registry.
- [MCP Inspector](https://modelcontextprotocol.io/docs/2026-07-28/tools/inspector)
  provides interactive MCP server testing. A generic tool browser would need
  more value than inspection alone.
- [FastMCP authorization](https://gofastmcp.com/servers/authorization) provides
  component visibility and permission checks. Toolgraph can complement this
  with pre-change impact analysis while preserving its advisory boundary.
- [OPA explanations](https://www.openpolicyagent.org/docs/rest-api) and
  [decision logs](https://www.openpolicyagent.org/docs/management-decision-logs)
  already support policy debugging and audit. Explainability itself is not
  unique; Toolgraph's opportunity is a domain-specific workflow requiring
  less manual assembly for agent/tool/resource policy questions.

These are selected official references checked on 2026-10-16, not a full
competitor feature matrix. They do not establish that other products lack
the proposed capabilities. The priorities below are our inference from the
references and the current Toolgraph workflow.

### Observed user journey

Ran seven installed-CLI commands in a disposable, isolated quickstart:
`example init`, `init`, `crawl`, strict manifest ingest, Markdown audit,
selection explanation, and policy compile. All exited 0. The demo used a
local MCP fixture and Ladybug, with no gateway, paid API, or external server.

The audit rendered `Status: FAIL`, a finding-count table, and a JSON evidence
block for the intentional `publish_note` DENY. `selection-explain` returned
structured reasons; compilation produced one eligible and one rejected tool.
These are correct outcomes, but the user still has to translate findings
into a review action and distinguish audit findings from runtime enforcement.
The walkthrough establishes command behavior, not how quickly a new human
user learns the product.

The source also shows reusable foundations rather than missing core features:

| Existing capability | Remaining user task |
|---|---|
| Offline quickstart and local backend | Move from synthetic demo governance to the user's own fleet |
| Markdown audit and selection explanations | Decide which evidence to inspect and what to do next |
| Structural manifest dry-run and blast radius | Compare actual before/after eligibility over the proposed manifest |
| Graph state and artifact digests | Explain meaningful changes and incomplete coverage across repeated reviews |
| Observation-based proposals and accepted review plans | Review intent explicitly; observations must not become automatic grants |

### Registered product improvements

| Order | Issue | Concrete user result | Reuse / boundary |
|---|---|---|---|
| 1 | [#156 Actionable evidence reports](https://github.com/memtomem/toolgraph/issues/156) | See the affected agent/tool, evidence, and next review step without decoding JSON | Extend human rendering; preserve machine contracts, verdicts, and opt-in gate semantics |
| 1 | [#157 Own-fleet guided onboarding](https://github.com/memtomem/toolgraph/issues/157) | Reach a useful finding or explicit evidence gap in the user's own environment | Start with explicit `servers.yaml`; separate facts, hints, unanswered questions, and reviewed candidate governance |
| 2 | [#158 Semantic policy impact preview](https://github.com/memtomem/toolgraph/issues/158) | See what will become allowed/rejected before applying a change, and export regression expectations | Reuse selector semantics and existing structural diff; evaluate hypotheticals without writing the live graph |
| 3 | [#159 Evidence baseline comparison](https://github.com/memtomem/toolgraph/issues/159) | Focus repeat reviews on new, persistent, resolved, or unknown changes | Explicit private snapshots with scope/identity compatibility; omissions must not silently resolve findings |

The ordering reflects delivery dependencies, not security severity. Semantic
impact preview is the strongest differentiation experiment; reports and
own-fleet onboarding make that value discoverable. Baseline comparisons give
users a reason to return after the first successful audit.

For example, a proposed grant change should yield a review statement such as:
"ci-bot gains eligibility for git::show on the declared scope; the resource
path and policy evidence are shown below." That is an illustrative future
output, not a current finding. A DENY path or missing evidence must remain
visible instead of being hidden to improve the apparent success rate.

### Validation before expanding the service

The issues define future acceptance tests and pilot targets. None of these
targets has been measured with users yet:

- Report comprehension: at least four of five target users can identify the
  finding, evidence, and next diagnostic action within two minutes.
- Activation: at least four of five reach a useful own-fleet finding or
  explicit evidence gap within ten minutes after dependencies/auth are ready.
  Measure human elapsed time and manual edits separately from command time.
- Change review: reviewers correctly identify at least four of five seeded
  semantic regressions without applying the manifest to the live graph.
- Repeat use: identical baselines create zero false new findings; omissions
  remain unknown. Measure whether pilot teams return for a second comparison.

Start with local CLI/Markdown artifacts and opt-in pilot observation, not
automatic telemetry. A later hosted collaboration service could reduce
handoff effort, but tenancy, access controls, secret handling, and evidence
retention would require their own design and customer validation. A generic
graph dashboard, automatic LLM-authored permissions, additional gateway
enforcement, and many client importers are not first-slice requirements.

No additional ADR was accepted or created for these product hypotheses.
Issues #158 and #159 explicitly require a Proposed ADR before introducing
their new artifact and comparison contracts. ADR-0014 remains unchanged.
No runtime implementation, commit, push, or automatic PR comment was made.

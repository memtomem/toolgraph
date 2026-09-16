# v12 independent-review remediation — 2026-09-16

Base: `bbe296eabc9a0baa424b0bcad7feebcfbc661d9d`. This report describes local
working-tree changes following the Claude review of `66076e8..bbe296e`.
That review identified three Major and eight Minor/Nit findings; its bold
verdict line was not parseable by the dev-trio host. The findings were still
actionable. Its local-only artifact is
`.dev-trio/log/default/codex-20260916-203357.final.md` (ignored by Git).

## Findings and fixes

| Finding | Resolution |
| --- | --- |
| Doctor creates an absent Ladybug database | Connectivity and metadata use read-only acquisition. Owned Ladybug handles use native `read_only=True`, never populate the writable cache, and close after use. Missing files, including a disappearance during open, cannot be recreated. |
| Empty Ladybug database produces a dry-run traceback | Readers validate physical schema as well as the version. Missing schema produces a controlled `init-schema` instruction. Existing legacy schemas without a version stamp remain readable without migration. Dry-run uses one session for schema, graph-state bracket, and delta reads. |
| Exposure fields and warning decision are undocumented | ADR-0013, JSON Schema, and fixtures now define exposure records, warning rules, truncation and legacy omission semantics. The ecosystem document distinguishes control-preflight from single-agent selection preflight. |
| Per-reader path-copying traversal is expensive | Parent pointers replace path copying. A reverse reachability pass prunes dead branches; absent writers skip traversal. Forward node/edge work has a cumulative 65,536-step budget per attempt. |
| Candidate count looks exact after truncation | The compatible field name is retained and explicitly documented as a discovered lower bound when truncated. |
| Byte cap precedes redaction | Records are sanitized before accounting. The 64,000-byte cap includes array brackets and comma-space separators under default `json.dumps` serialization. |
| Global resource evidence redaction is unexplained | Comments document reserved evidence keys and distinguish them from opaque identity fields. |
| CLI does not expose complete dry-run delta | `ingest-manifest --dry-run --json` emits the full report, including revoked grants and rejection warnings. `--json` without dry-run is rejected before graph access. |
| Proposal output errors and conflicting options | Output `OSError` becomes a redacted CLI error. `--json --output` is rejected before graph access. |
| Redundant doctor assignment | Removed. |
| Historical report and ignored-file links | The report now distinguishes its validation snapshot from the subsequent commit/push, records the delivered SHA and CI, and labels ignored artifacts as local-only filenames. |

Witness compression also now enforces 32 total entries, including the omission
marker. The previous implementation returned 33 entries.

## Validation

- Full local suite after all follow-up fixes: **733 passed**, two existing testcontainers deprecation warnings.
  Command: `uv run --no-sync pytest -q --tb=short`.
- `uv run --no-sync ruff check .`: passed.
- `git diff --check`: passed.
- New regression coverage includes isolated Ladybug file preservation and
  read-only open races, dual-backend dry-run JSON, controlled CLI failures,
  loader-valid 4,096-node plans, traversal/record/byte boundaries, redaction,
  producer decision behavior and legacy schema acceptance.
- No current STM gateway smoke was rerun. These are local checks, not fresh CI.

## Independent re-review

Claude invocation `codex-20260916-214443` returned a canonical `SHIP` token but
also reported a new Major. That contradictory token was not treated as approval.
The local-only artifact is `.dev-trio/log/default/codex-20260916-214443.final.md`.
Follow-up fixes:

- Dry-run JSON scrubs every display string, including resource URI lists and
  nested warning/reason text, without mutating the underlying delta.
- Catalog query failures now report inspection/permission guidance instead of
  claiming that an initialized database needs `init-schema`.
- Corrupt or incompatible Ladybug files produce typed, controlled errors.
- A reentrant lifecycle lock protects borrowed read-only sessions from another
  thread closing their database; nested reads and concurrent close are tested.
- A real producer comparison now binds the golden exposure fixture. It found
  a missing second reader exposure, which was added to the example.
- Conservative traversal cutoff remains intentional and documented. JSON
  formatting remains consistent with the schema and fixture files; no separate
  formatting commit was created in this local remediation.

Claude invocation `codex-20260916-215344` returned canonical **SHIP**, with no
Blocker or Major. Its final file was read directly; the older wrapper leaves
the manifest verdict null, so that field is not used as acceptance evidence.
The remaining actionable minor points were then addressed: catalog outages
retain their typed classification (including native Neo4j outages), text-mode
ingest warnings/notices are redacted, and the lifecycle docstring prohibits
same-thread close inside an active session. Conservative cutoff remains the
documented behavior.

Confirmation invocation `codex-20260916-221545` (Claude, exit 0) reviewed the
final small delta and returned **SHIP**, with no Blocker or Major and no new
actionable finding. The remaining notes explicitly require no change or are
optional test-style suggestions. Exact local-only final artifact:
`.dev-trio/log/default/codex-20260916-221545.final.md`. The reviewer ran the
30-test follow-up module, Ruff, and diff checks; the PM's full final run passed
719 tests. This confirmation supplements the preceding whole-tree review.

No new commit or push has been made for this snapshot.

## Whole-tree review follow-up (`codex-20260916-222757`)

This later Claude run passed 719 tests but returned a bold, noncanonical SHIP
line alongside three Major findings. It was not accepted as an approval.
All actionable findings were addressed:

- The Ladybug helper explicitly documents serialization of all concurrent
  acquisitions for the whole session, including graph-state retry backoff.
  Retaining the simple lifecycle lock is intentional for diagnostics/previews;
  the helper is not a parallel request-serving API. Replacing this with a
  reference-counted handle manager was considered and is unnecessary for these
  callers. Existing concurrent-close and nested-read regressions remain.
- Neo4j catalog inspection requires `SHOW CONSTRAINTS` permission in addition
  to data reads; operator documentation now states this explicitly. No
  version-only fallback hides catalog inspection failure. The helper also
  documents ordinary Neo4j access/routing mode rather than claiming enforced
  read-only protection.
- Removed schema enforcement of the current producer's decision mapping.
  Legacy v1 results with either advisory decision remain valid independently
  of findings, rejection, missing agents or exposure evidence. Producer tests
  still enforce current warning behavior. Consumers must evaluate compatibility
  with their own advisory policy; unchanged consumer behavior is not assumed.
- Restored both original v1 fixtures byte-for-byte and added separate exposure
  fixture files. Both pairs have schema/digest tests, and the exposure pair is
  compared with actual producer output.
- Retained original formatting for existing schema content, removing unrelated
  formatting churn. The schema description explicitly labels the serialized
  byte cap as producer-enforced. The misplaced CLI import was moved.

Validation: full suite 733 passed (two existing warnings), Ruff and diff checks
passed. Claude follow-up `codex-20260916-224448` exited 0 and returned a canonical
plain **SHIP**, with no Blocker or Major. Exact local-only artifact:
`.dev-trio/log/default/codex-20260916-224448.final.md`. It independently ran 146
targeted tests. The older wrapper still leaves manifest verdict null; the
invocation's final file provides the verdict.

After review, optional presentation notes were addressed: new scalar schema
blocks were compacted with parsed JSON equality checked, ADR-0013 documents
historical main snapshots emitting 33-entry witnesses, and README links to the
preview/diagnostic permission requirements. These final edits change formatting
and documentation only, not producer or schema semantics.

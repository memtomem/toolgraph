# Implementation remediation and verification

Historical result dated 2026-09-07. [Korean companion](ko-2026-09-07-remediation-verification.md).
Baseline: `45b594872cf0d94635aa26db8a7f6fe6464159ae`; scope was the subsequent
local worktree changes. The [original review](2026-09-07-implementation-review.md)
remains a record of its original findings. Later integration/CI evidence is in
[the stabilization report](2026-09-08-stabilization.md).

## Results

R1-R9 were addressed with 82 new regression cases; all 496 tests passed against
real isolated Neo4j and Ladybug. This dated result did not claim remote CI,
publication or external consumer integration.

| Finding | Correction | Evidence |
| --- | --- | --- |
| R1 | Skip empty struct-list upserts only; retain reconciliation/revocation/generation. | Eight tools/resources/policies combinations, nonempty-to-empty, revocation, rollback after mutation on both backends. |
| R2 | Field-aware artifact identity handling; preserve agent/principal/run/candidate/tool identities and reject credential-shaped values. | Distinct URI query/fragment decisions, saved preflight readback, control principal identity, rejection before graph access. |
| R3 | Common evidence redaction for review current_paths. | Actual CLI JSON strips resource/provenance/reason URI credentials. |
| R4 | `v2:` plus SHA-256 of canonical JSON tuple; distinct null resource; resolve duplicates last-entry-wins before UNWIND. | Both colliding exceptions survive, last reason wins, null/string distinction, legacy-node re-ingest. |
| R5 | Strict models and JSON-integer normalization; required maximum_cycles, explicit null rejection and bool/int separation. | Missing/coerced/null variants rejected; 1.0/0.0/2.0 accepted as JSON integers; additive/major/topology cases retained. |
| R6 | Sorted validated catalog in 4,096-item chunks inside one strict bracket; exact evaluation coverage. | 0/4,095/4,096/4,097, whole-attempt retry on late drift, failure after five unstable attempts, malformed/incomplete/conflicting coverage rejected, old output preserved. |
| R7 | Lazy settings and explicit configuration selection; concise typed CLI failures. | Fresh subprocess help/version and valid explicit config survive broken defaults; broken explicit config fails without traceback. |
| R8 | Every state read uses the supplied reader; optional EvaluatingGraphReader protocol. | Independent injected reader avoids global DB; old adapters without evaluate still work. |
| R9 | Existing RFC3339 offset validation before I/O; jsonschema[format-nongpl] dependency. | Naive/undefined/30-second offsets rejected; UTC/minute offsets pass an explicitly installed date-time checker. |

Cases live in `tests/test_review_remediation.py`; common fixtures in
`tests/conftest.py` require Ladybug instead of silently skipping it. Existing
backend-specific tests remain.

## Verification

| Check | Result |
| --- | --- |
| Ruff and diff whitespace | PASS |
| Targeted regression suite | 82 passed, 20.21 seconds |
| Full suite | 496 passed, three warnings, 54.05 seconds; no skips |
| Lock check | 101 packages, unchanged |
| Runtime/extras audits | No known vulnerabilities at lookup time |
| Wheel/sdist and Twine | Both distributions passed |
| Artifact allowlist/rebuild | 49 entries, wheel byte equality |
| Independent install | init, crawl, strict ingest, selection, compile all exit 0 |

Final wheel SHA-256:
`64e7cb2357fc7d70f3c092d675da55f6b4985ab04ff68b994cd911b02197d47b`.
Test environment: Python 3.12.11, Neo4j 5.26-community and Ladybug 0.18.3.
Independent installation used no editable checkout and resolved MCP 1.29.1,
Neo4j driver 6.3.0 and Pydantic 2.13.5. The quickstart allowed read_note,
rejected publish_note and wrote the bundle with mode 0600.

The three pre-existing warnings were two deprecated testcontainers wait APIs
and a settings lifespan forward reference. Docker access was retried after a
sandbox failure. Development fixture mistakes (nonexistent `python -m toolgraph`
entrypoint and Ladybug primary-key SET) were corrected before final validation.
The Korean companion retains the historical temporary artifact locations.

## Compatibility and application

Schema v1, reason vocabulary, golden fixtures and normal-input digest contracts
remain. Credential-shaped identities, missing required fields and coercion-based
invalid inputs now fail. Existing GraphReader implementations need not add
evaluate. No physical schema bump or database reset is needed; legacy exception
nodes remain readable through their agent/tool/resource/policy properties.

Re-ingest the original governance manifest to rebuild exception keys and restore
rows lost to prior collisions, verify the result and recompile bundles. Lost
rows cannot be reconstructed from old artifacts without the source manifest.

```bash
toolgraph --config <runtime-config.json> ingest-manifest \
  --governance <governance.yaml> --strict-drift
toolgraph --config <runtime-config.json> policy compile \
  --agent <agent-id> --profile review --output <policy-bundle.json>
```

No user production database was migrated. At this milestone, ingest batching,
large-scale benchmarks, external consumer integration, remote CI and deployment
were excluded, and no commit/push/PR had yet been performed. Later milestones
must provide their own evidence; Toolgraph remained advisory throughout.

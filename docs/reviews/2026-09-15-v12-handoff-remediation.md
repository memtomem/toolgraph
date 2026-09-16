# v12 handoff remediation — 2026-09-15

## Delivery state

- Working tree based on `32f6be2da07cf1fd49c084ba97a98b4975a43b37`.
- Review base: `66076e824aae1dedebfaee2ad0ac6452d521f428` (the six v12 commits plus current WIP).
- The original two uncommitted files were carried forward. At the time of this validation snapshot, no commit, push or deployment had been performed. The reviewed changes were subsequently committed as `bbe296eabc9a0baa424b0bcad7feebcfbc661d9d` and pushed to main on 2026-09-15; CI run `34955079546` passed.
- Previous review: `codex-20260915-070318-33453.final.md`, NEEDS-FIX (1 blocker, 5 majors).

## Remediation

| Finding | Result |
| --- | --- |
| Doctor credential-bearing exceptions | Configuration inspection/loading, connectivity, schema and graph-state errors are redacted in both text and JSON. Config detail strings and displayed backend names are also scrubbed. |
| Doctor malformed configuration crash | Invalid config returns an unhealthy report before connecting; inspection and lazy settings failures return controlled reports. |
| Nullable expected-exception sorting | Additions, removals and reason updates use an explicit nullable-aware ordering. |
| Empty governance bindings | Only nonempty bindings retain authored resources/tools; projected resource deletion matches real ingest. |
| Incomplete composite identity | Default refusal; explicit unverified opt-in permits missing identity but never conflicting identities/digests or malformed fields. JSON and Markdown report the same provenance status. |
| Retired tools accepted by revalidation | Strict selector checks hypothetical grant additions using current exposure, mapping and DENY facts inside the graph-state retry bracket. Missing agents and redundant grants are excluded with explanations. |

The approved provenance choices and public output fields are documented in
[the policy proposal guide](../policy-propose.md). Current revalidation does not
establish historical provenance. Generation mismatch requires revalidation
separately from unverified opt-in. Existing ambiguous-grant protection and
explicit zero-call removal behavior are retained.

## Validation

- `uv run --no-sync pytest -q --tb=short`: **689 passed, 2 warnings in 60.18s**.
- Includes **62 new regression cases**, with graph behavior exercised against isolated Neo4j and Ladybug.
- Covers nullable exception add/remove/update parity, empty binding pruning,
  safe doctor failures, aliases and incomplete/contradictory provenance, strict
  DENY/drift/mapping checks, 4,097 candidates, and actual state-change retries/exhaustion.
- `ruff check .`: passed.
- `git diff --check`: passed.
- Warnings are the existing testcontainers Neo4j readiness/wait deprecations.

## Gateway evidence boundary

The existing `/private/tmp/toolgraph-smoke-artifacts/policy-bundle-gateway-summary.json`
records `status: pass` for clean source trees:

- Toolgraph: `32f6be2da07cf1fd49c084ba97a98b4975a43b37`.
- memtomem-stm: `5b0d20746326616efa9f5335843937c88626f217`.

The currently inspected STM checkout is `main` at
`53eb0c5b76c2e625d2d7cc066a3de6a424ce519a`. That producer/consumer combination was
not rerun. The historical smoke is not evidence for this WIP or the current STM
checkout. Remote CI, release and deployment were not validated in this task.

## Final independent review

**SHIP — 0 blocker, 0 major, 0 minor findings.** The invocation final, parsed result, caller receipt and manifest agree.

- Run: `codex-20260915-184759-40740-3ef7`; completed `2026-09-15T18:49:49+09:00`.
- Final: `codex-20260915-184759-40740.final.md` (local ignored artifact under `.dev-trio/log/default/`). SHA-256: `60930e0db37bc7f3af8209010551bd2ecbd1e827827b0be15283d49798350a38`.
- Result: `codex-20260915-184759-40740.review.json` (local ignored artifact under `.dev-trio/log/default/`). SHA-256: `047ca7bcca9affb207bd84e1cb2632e501d97fb41bd0b5a634eefbf325aea064`.
- Manifest: `codex-20260915-184759-40740.manifest.json` (local ignored artifact under `.dev-trio/log/default/`). SHA-256: `bb71dcaf19ece665d76e9acbccaf60e0b63704083bf9e6e7d1f2e1985c40a3cf`.

The reviewer independently ran **102 passing focused tests, 41 deselected, and 1 setup error** caused by sandbox-denied Docker access. The full 689-test run above was executed with Docker access and includes isolated Neo4j coverage. Neither run validates the current gateway consumer.

## Reviewed implementation snapshot

The reviewer checked all **20 files** against this exact snapshot; their hashes were checked again after review. Snapshot digest: `c69c2bffeb9d87897a760d17e15346c1376a661f4f7c040b513e5a2881b49861`.

This report was added afterward as the evidence index; it is not included in the reviewed implementation snapshot. Review logs are local ignored artifacts. The file hashes below preserve the exact source target independently of those logs.

<details>
<summary>Source target hashes</summary>

```json
{
  "base": "66076e824aae1dedebfaee2ad0ac6452d521f428",
  "head": "32f6be2da07cf1fd49c084ba97a98b4975a43b37",
  "files": {
    "README.md": "adfdb04ca3e7a6cce6258280453e2730f8163ccaa3b4081ef1181cb57b9a8b54",
    "contracts/observation-window.schema.json": "49916b673deb3d03210eee2d94df5eee2dcff74436059095a25cd2af8091f01d",
    "docs/policy-propose.md": "d684f34c171efa03f616d88dd49012f18fe812a6c220c162c291357c6d4201a7",
    "scripts/policy_bundle_gateway_smoke.py": "6858a740ee9d8276329746dacafb32ff77dfa7218c05d6b173acd75ee8174f19",
    "tests/test_control_plan.py": "379523133c5b6f6a9ec27ff4c5a8731cdb832e6aeb5fdfec18278c08b44ee159",
    "tests/test_handoff_regressions.py": "c9ae632253918d9bfee1c73ba25179300a5ee70274fdfee60fd36a87e0d0b661",
    "tests/test_health.py": "7dfa6fbc20e6c1aadd44bfb7aee5b2854b31ac529e17cabd969dcc6efafe3674",
    "tests/test_manifest_diff.py": "e95db635fbe3bb07890adbd47b614f28fff6fe3e4a0095a5491a9f3f55342cde",
    "tests/test_policy_bundle.py": "5de78f0aef123c39bcab546e51a3a4e07f8be9f7505de208d5be0a9d7ae696ca",
    "tests/test_policy_propose.py": "c9b58870c99688d7cce377e714385981e8a3e56fec6fbd8478054e6c0610230d",
    "toolgraph/artifact_safety.py": "4329a643219642123737a7a4db264c1b07b54285a1cb7a69267ed5689b21c887",
    "toolgraph/cli.py": "1c615faab66eb5742e6c3ef907f549d835c61d753288e2f8af3f02653442973b",
    "toolgraph/config.py": "7655867792529924aaab5f7075461db4e8f5b5d3796efbb376505ce2e3813335",
    "toolgraph/control_plan.py": "073bf277f07c07f0c21600a5309e823d577a299fb81197f58e4aa1ba14a511f5",
    "toolgraph/graph/driver.py": "a59b6b34bf792ace0ef4095cd008612e591767a45d010a15c558863ea9d13d7a",
    "toolgraph/graph/schema.py": "0341ffccf023d0c8750b24fa480fc32a0e55cdb8f9e0912b8476ae20bf7b70e2",
    "toolgraph/health.py": "a7bdcef01c62bec4f009c461916bad809a21b6cbddf8e1b81b2f7d7e68c814f0",
    "toolgraph/manifest/ingest.py": "2b48c7dfb930b01b931d4afcd0e06a7531ee6ede9f65c3f97df22be4ebde7cf8",
    "toolgraph/policy_bundle.py": "6a0db6a4f6ddf02e5fb070c67a2ce2dff12afde8cd1f8beb3f7019fa2ac722aa",
    "toolgraph/policy_propose.py": "9f6e3af4e72169c576ccaf2b67a19077d3e591bbdd6649f67f4c2989342ed567"
  },
  "sha256": "c69c2bffeb9d87897a760d17e15346c1376a661f4f7c040b513e5a2881b49861"
}
```

</details>

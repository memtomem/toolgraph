# Public release readiness — 2026-09-08

Target: Toolgraph **0.0.1 / Alpha**, existing PR #87 (`release/v0.0.1-prep`),
updated with main `c173ae7`. This report distinguishes readiness from publication.

## Prepared changes and validation

- PyPI-first README and paired beginner guides; explicit Ladybug/Neo4j backend
  description. CHANGELOG carries the candidate date 2026-09-08. If the release
  candidate changes before rehearsal, revalidate and update its date deliberately.
- Current release checklist replaces stale August instructions. Keep history
  and contributor attribution; publish companion architecture and verification
  records as approved. Generalize workstation-specific example descriptions.
- English editions of the two September 7 reviews link preserved Korean
  companions; historical measurements and findings retain their original scope.
- `scripts/verify_index_release.py` verifies both index metadata and downloaded
  wheel/sdist bytes against a local candidate. It rejects partial/duplicate file
  sets, wrong project/version, yanked releases, unsuitable URLs and hash mismatch.
  The runbook separates TestPyPI project selection from PyPI dependencies and
  requires comparison before production environment approval.
- PASS: **521 tests, three existing warnings, 49.25 seconds**, isolated real
  Neo4j and Ladybug. Includes ten new published-artifact regression cases.
- PASS: Ruff, diff check, frozen lock (101 packages), runtime/extras/dev audits,
  wheel/sdist build, Twine, 49-entry allowlist and same-environment wheel rebuild.
- PASS: standalone wheel installation (no editable checkout) and all seven
  quickstart commands. read_note was eligible and publish_note rejected with
  DENY graph-path evidence. The fresh environment resolved MCP 1.30.0, Neo4j
  driver 6.3.0, Pydantic 2.13.5 and Ladybug 0.18.3; this is separate from the
  locked full-suite environment.
- Wheel SHA-256: `ec7739d5aaf3dc7141484948b44781bc151c52482eb5e7d6d69222ada6d74951`.
  This is a local candidate hash, not a TestPyPI upload claim.

## License and CLA reference

Compared against the memtomem checkout at `8c966105`: LICENSE and
`.github/cla-check.py` are byte-identical. CLA Sections 2-8 retain the same rights
and representations. Toolgraph's existing scope is repository-specific and its
signature store is independent; these were not broadened. Workflow differences
are the Toolgraph document URL and removal of a memtomem-specific historical
comment. No legal text, signature or attribution was fabricated or replaced.

## Disclosure audit snapshot

The audit fetched all advertised PR head refs into a local audit namespace and
scanned all locally reachable Git objects, including local branches. Coverage:

| Surface | Inspected |
| --- | ---: |
| Reachable commits | 221 |
| Unique Git blobs | 666 |
| Issue/PR records | 93 (includes 73 PRs) |
| Issue comments / inline review comments | 52 / 0 |
| Body/comment edit revisions | 79 |
| Actions run log archives | 309 (178 nonempty, 131 empty) |
| Valid Actions artifacts | 8 |
| Expired artifacts | 25 — unavailable, not passed |
| Downloaded archive entries | 1,194 |
| Decompressed archive bytes | 19,134,467 |

Pattern checks covered credential-shaped tokens, private-key headers, URI
userinfo and personal local paths. No token/private-key pattern matches were
found. Twenty-five Git blob matches were four distinct synthetic URI fixtures,
including the deliberate `u:p` example; these are retained regression evidence.
Thirty-seven log files matched local path syntax; a follow-up scan confirmed
only hosted-runner home paths. Do not interpret these scoped patterns as proof
that every possible secret is absent.

**HOLD: PR #35 has four historical body edits containing personal local paths.**
The current body is clean, but the edits are readable after publication. The
exact four edit IDs and timestamps are retained in the private audit workspace;
no personal paths are copied here. Removing current text again would not fix
this. Targeted historical edit removal requires a deliberate destructive action;
no Git history rewrite, PR deletion or current-body deletion is proposed.

Repository metadata: 8 remote branches, no tags/releases, no deploy keys or
webhooks, 3 collaborators (one admin, two writers), no teams. Actions enabled;
default workflow token permissions read-only and PR approval by workflows off.
No Actions secrets or variables. Wiki and Pages disabled; Discussions empty.
`pypi` environment absent at inspection. Private-plan rulesets/fork approval
configuration were unavailable; private vulnerability reporting returned 404,
which is not evidence that the reporting channel works.

Limitations still to close or explicitly record: rendered job summaries,
attachments outside API text, installed Apps/Packages inventory (API requests
returned 401/403; Packages requires read:packages), and any new
refs/runs/text added after the audit snapshot. Review these before changing
visibility. The browser runtime reported no connected browser, so authenticated
UI inspection could not fill these gaps. Local full evidence is under a private temporary audit directory;
its summaries retain identifiers, hashes and counts, never matched credentials.

## External publication status and next actions

- PyPI and TestPyPI JSON/simple lookups returned 404: no published Toolgraph
  distribution was found. HTML Client Challenge responses are not project data.
- The owner reports that both Pending Trusted Publishers are registered. Browser
  management pages require authentication; actual OIDC upload is NOT RUN.
- Visibility remains private. Environment/reviewer/tag restrictions, main branch
  protection, public security features and final live install verification are
  NOT RUN. The publisher cannot be inferred from YAML alone.
- Complete historical-edit handling and the remaining surface review first.
  Then public transition/settings, PR #87 merge with green CI, `test-v0.0.1`,
  published-file verification and clean install, same-source `v0.0.1`, production
  digest approval, PyPI readback/install and GitHub Release.
- No paid provider canary, MCP 2.x migration, observation-based grant proposal,
  production strict rollout, release tag or external package upload occurred.

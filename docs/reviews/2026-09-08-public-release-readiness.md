# Public release readiness — 2026-09-08

> **How to read this document.** Every section up to *External publication
> status* is a **historical snapshot** written before publication, and its
> statements about visibility, environments and pending work were true when
> written, not now. Statements such as "no deletion has been performed" and
> "repository visibility remains private" describe the state at audit time. The
> current state is recorded in
> [External publication status](#external-publication-status-and-next-actions);
> where the two disagree, that section is authoritative. The earlier text is
> preserved so the evidence behind each decision stays legible.

## Follow-up exhaustive inventory audit

The follow-up examined candidate `6c3e841` and all advertised PR head refs.
The owner clarified that deletion authorization covers **only the four July 12
PR #35 body revisions**, not the PR itself. No deletion has been performed.

| Surface | Follow-up coverage |
| --- | ---: |
| Reachable Git commits / unique blobs | 222 / 679 |
| Issue/PR records | 93, including 73 PRs |
| Issue comments / PR reviews | 52 / 6 |
| Inline review comments / commit comments | 0 / 0 |
| Available body, comment and review revisions | 84 |
| Actions runs / available artifact archives | 311 / 8 |
| Archives downloaded and inspected | 319, none failed |
| Archive entries / decompressed bytes | 1,375 / 19,823,044 |
| Unique check runs / annotations | 1,482 / 1,139 |
| Attachment URLs matching GitHub upload formats in inspected text | 0 |

Expanded patterns covered macOS, Windows and Linux home paths; additional
OpenAI-style, PyPI, Hugging Face, GitLab and npm tokens; and common private IPv4
addresses, alongside the original credential, key-header and URL checks.
The conversation/revision/check-output pass found only the same four PR #35
revisions. No new personal-path or token-pattern finding was identified.
The Git scan had 26 URI-userinfo findings representing the previously reviewed
synthetic fixtures, including the preserved Korean review copy. The initial
log scan's macOS paths were hosted-runner paths. All 34 additional Linux-path
log findings were individually classified as `/home/dependabot/`.

Current workflows were also reviewed for action pins, permissions, release
artifact handling and CLA execution. The CLA workflow executes default-branch
code with checkout credential persistence disabled. Release build and OIDC
publication remain separate jobs. Repository-wide SHA pin enforcement is off,
although current action references use commit pins. The `pypi` environment is
still absent; repository visibility remains private. Discussions is enabled
with zero entries, and Wiki/Pages are disabled.

API authentication works. Live GraphQL introspection exposes `userContentEdits`
for reading but no public mutation for deleting those revisions. GitHub's
documented removal path is the authenticated web UI: edited, select revision,
Options, Delete revision from history. See
[GitHub's edit-history documentation](https://docs.github.com/en/communities/moderating-comments-and-conversations/tracking-changes-in-a-comment).
PR deletion, closing, and rewriting the current body were rejected as
substitutes.

**RESOLVED 2026-09-08.** The owner deleted the four July 12 PR #35 body
revisions through that web path, between `23:29:09Z` and `23:29:46Z`. API
readback confirms all four carry a non-null `deletedAt` and return the literal
string `deleted` in place of their content. The two August 23 revisions were
left intact as authorized and carry no personal path. The current PR body is
also clean. Editor identity and edit timestamps remain visible by design, which
GitHub documents and which discloses nothing beyond the existing commit
metadata. PR #35 itself was neither deleted nor closed, and no Git history was
rewritten.

Coverage limits: 25 expired artifacts remain unavailable. Three locally
reachable commit SHAs returned 422 when querying remote check runs; they were
still included in the Git content scan. Check-run output and annotations were
inspected, but independently rendered job summaries were not accessible.
Installed Apps requires different authentication (401), Packages requires
`read:packages` (403), and secret/code scanning is not enabled. Private-plan
ruleset/fork-approval restrictions remain. No pattern scan proves absence of
all secrets; unrecognized upload URLs and image/OCR content are not covered.
Repository visibility and package publication were not changed by the audit
itself; the only mutation performed under it is the authorized revision
deletion recorded above.

**Disposition of the inaccessible surfaces, 2026-09-08.** The owner accepted
the expired artifacts, independently rendered job summaries, installed
Apps/Packages inventory and image/OCR content as residual risk rather than
blocking on them. The expired artifacts are unavailable through the audited
GitHub interfaces and their contents are unverified; that establishes what this
audit could not retrieve, and does not exclude copies downloaded before
expiry or retained elsewhere. The remaining surfaces could not be reached with
the available credentials. This is an explicit acceptance of residual risk, not
a clean result.

**The public release hold is therefore lifted.** Remaining work is the
visibility transition, repository settings, and the release sequence in
[the release runbook](../releasing.md); those are tracked separately from this
audit.

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

**PR #35 had four historical body edits containing personal local paths.**
The current body was clean, but those edits would have been readable after
publication. The exact four edit IDs and timestamps are retained in the private
audit workspace; no personal paths are copied here. Removing current text again
would not have fixed this, so targeted historical edit removal was required.
**This was carried out and verified on 2026-09-08** — see the follow-up section
above. No Git history rewrite, PR deletion or current-body deletion was used.

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

**Toolgraph 0.0.1 was published on 2026-09-09.** The sequence below is a record
of what ran, not a plan.

| Step | Outcome |
| --- | --- |
| PR #35 revision deletion | 4 of 4 deleted between `23:29:09Z` and `23:29:46Z` on 2026-09-08, each verified by GraphQL readback showing a non-null `deletedAt` |
| Visibility | public; the rulesets API returned an empty list, so none needed restoring |
| `main` branch protection | required check `ci-required` (bound to the GitHub Actions app), strict, force-push and deletion disabled |
| Security features | secret scanning, push protection, Dependabot alerts and private vulnerability reporting all enabled; 0 alerts at the time of check |
| `pypi` environment | required reviewer `memtomem`, admin bypass disabled, tag policies `test-v*` and `v*` |
| PR #87 | merged as `cfc83adfa708917cc61780f58d477be90e359720` with all 14 checks green |
| Local release gate | 521 tests, ruff, three audits, twine and byte-identical sdist rebuild all PASS |
| `test-v0.0.1` rehearsal | tag at `cfc83ad`; run [34293077841](https://github.com/memtomem/toolgraph/actions/runs/34293077841); TestPyPI upload succeeded; index verification PASS |
| Clean-room install | wheel digest matched; seven quickstart commands ran; `read_note` eligible and `publish_note` rejected with DENY path |
| `v0.0.1` production | tag at the same `cfc83ad`; run [34294526054](https://github.com/memtomem/toolgraph/actions/runs/34294526054); PyPI upload succeeded |
| PyPI readback | index verification PASS; fresh-venv install and quickstart acceptance repeated |
| GitHub Release | `v0.0.1 — Alpha`, prerelease flag set |
| Release-tag protection | ruleset `release-tags` added after review: creation, update and deletion blocked on `refs/tags/v*` and `refs/tags/test-v*`, admin bypass |

Both distributions carry PEP 740 attestations whose publisher is repository
`memtomem/toolgraph`, workflow `release.yml`, environment `pypi`. Package
metadata reads Apache-2.0 with `requires_python <3.15,>=3.12`.

| Artifact | SHA-256 |
| --- | --- |
| `toolgraph-0.0.1-py3-none-any.whl` | `ec7739d5aaf3dc7141484948b44781bc151c52482eb5e7d6d69222ada6d74951` |
| `toolgraph-0.0.1.tar.gz` | `55e19405cd4eb664bc9afbab04f457e1fad5d250e73ccf6b3a910170e71d0f5b` |

These digests are identical across the local build, the CI rehearsal build and
the production build, so the reviewed source and the published bytes correspond.

**Order of the production digest comparison.** The production build artifact
was downloaded and compared against the already-published TestPyPI bytes while
the publish job sat in `waiting` on the `pypi` environment, and the approval was
given afterwards. The verifier reported `status: pass` on both indexes. The
retained evidence for that ordering is this record and the run timeline; the
comparison itself was run locally and its output is not stored as a build
artifact, so it is not independently reproducible from GitHub alone.

**Known exceptions in the access controls, recorded deliberately.** Branch
protection on `main` sets `enforce_admins=false` and
`required_approving_review_count=0`. Either write collaborator can therefore
merge their own pull request once `ci-required` passes, including one that
changes a workflow, and the administrator can bypass the required check
entirely. `strict=true` governs branch freshness, not independent review. On the
`pypi` environment, `prevent_self_review=false` is intentional: the owner is the
only reviewer, and preventing self-review would make owner-triggered releases
impossible. Independent review of a release would require adding a second
reviewer. Fork pull requests use the `first_time_contributors` approval policy,
which is weaker than requiring approval for all outside collaborators.

Not done and deliberately out of scope: paid provider canary, MCP 2.x
migration, observation-based grant proposal and production strict rollout.

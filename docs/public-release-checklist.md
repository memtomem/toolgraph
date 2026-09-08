# Public release checklist

Release target: **0.0.1, Alpha**. Updated 2026-09-08. Use this checklist for
readiness and [the release runbook](releasing.md) for commands. The release
candidate is prepared in PR #87; do not reuse older issue text as instructions.

## Current baseline and disclosure decisions

- Main `c173ae7` contains PR #91 and #92. Its CI run `34205706702` succeeded;
  the 511-test stabilization and pinned consumer checks are recorded in
  [the stabilization report](reviews/2026-09-08-stabilization.md). The previous
  Actions billing block is historical, not a reason to change visibility.
- Keep this repository and its history. Do not rewrite contributor identities,
  commit trailers or historical SHAs. Removing a file from main does not remove
  its old revisions, PR refs, comments or edit history.
- The owner approved publishing the companion architecture and verification
  records: retain SyncMill/Tracegraph names, source attribution and contract
  SHAs. Remove unnecessary personal workstation paths and private operational
  details from current examples. Credentials are not part of that approval.
- Apache-2.0, contributor attribution and the explicit vendored Tracegraph
  license remain. Compared with the memtomem reference checkout `8c966105`,
  LICENSE and `.github/cla-check.py` are byte-identical; CLA Sections 2-8 retain
  the same grants and representations. Keep Toolgraph's pre-existing project-only
  scope and separate signature store. The workflow differs only in its document
  URL and a repository-specific historical comment. The earlier unreachable #69 reference is not an executable
  prerequisite; review the actual license and provenance in the repository.
- English is the default documentation language. The beginner guides and the
  two dated implementation review reports have explicitly linked Korean pairs.
  Historical reviews describe their original source snapshot, not current bugs.
- First-release scope excludes MCP 2.x and pending dependency major updates,
  observation-driven policy proposals (#88/#89), paid model canaries and a
  production strict rollout. Open PRs do not need to be merged merely to publish.

## What a public reader can already reach

Two facts govern everything else in this document, and they were nearly missed:

1. **Deleting a file does not remove it.** Under the no-history-rewrite
   decision below, any blob still reachable — from a retained branch, tag, PR
   ref or unchanged history — stays readable via `git show <commit>:<path>`.
   Removing something in a new commit only changes the branch tip and the next
   sdist. (Objects that become genuinely unreachable can eventually be garbage
   collected; nothing here relies on that.)
2. **Open branches carry their own copies.** A file removed on one branch is
   still at the tip of every other open branch until those are merged or
   deleted.

Any claim that something was "made private" by deleting or moving it is false
unless history is rewritten. See "Internal operational material" below, which
was originally filed under exactly that mistaken assumption.

## Before visibility changes

1. Freeze the release scope and record the reviewed SHA and remote refs.
   Inspect reachable Git objects, PR/issue text and revisions, attachments,
   Discussions, Actions logs/artifacts, and repository settings. Record actual
   coverage and unavailable surfaces in the release evidence. A pattern scan
   cannot establish that every possible secret is absent. Expired artifacts
   must be recorded as unavailable, not passed.
2. Keep synthetic redaction fixtures, illustrative local paths and hosted-runner
   paths when they are necessary evidence. Classify matches before deleting.
   Do not publish a newly discovered secret; resolve it before the flip.
3. Check collaborators/teams, deploy keys, webhooks, installed Apps, workflow
   permissions, environments and secrets metadata, Wiki/Pages/Packages and
   Discussions. Existing history decisions cover attribution, not credentials.
4. Check both PyPI indexes again. A JSON/simple API 404 means no published
   project was found; it does not reserve a name. A `Client Challenge` HTML
   response is not a package page. Pending Publishers do not reserve names.
5. Confirm the owner has authorized the public transition. Read back the new
   visibility, restore any push rulesets disabled by the flip, and configure
   `main` protection with the stable `ci-required` check. Reuse recent reported
   CI success; do not create a dummy commit solely because visibility changed.
6. Enable supported secret scanning/push protection, dependency alerts and
   private vulnerability reporting. Verify the reporting form; the email in
   SECURITY.md remains the fallback. Check fork-PR workflow approval settings.

## Before the first tag

- Create and read back the `pypi` environment, reviewer and release-tag
  restrictions described in the runbook. A workflow reference alone may create
  an unprotected environment and is not evidence of enforcement.
- Configure independent Pending Trusted Publishers on TestPyPI and PyPI for
  `memtomem/toolgraph`, `release.yml`, environment `pypi`, project `toolgraph`.
  An owner confirmation is recorded separately from successful OIDC publication.
- Finalize PR #87: current main, PyPI-first README and both beginner guides,
  actual release date in CHANGELOG, clean local release gate and current CI.
  Do not merge the PyPI-first text before the publication prerequisites are ready.
- Inspect the actual wheel and sdist again. The sdist intentionally excludes
  the upstream tests/docs/scripts and lockfile; it is a build input, not a full
  source checkout for downstream test execution. Count entries from the current
  artifact, rather than reusing old counts of 48 or 49.
- Follow the runbook: TestPyPI upload and clean install, compare the actual
  published hashes with the production candidate before environment approval,
  then PyPI and GitHub Release. Same source SHA alone does not prove same bytes.

## Completion

Record source/tag SHAs, workflow IDs, index metadata and distribution digests,
standalone quickstart results, and the final public/security/environment state.
If any required check is unavailable or fails, report HOLD with that exact
reason instead of claiming release success. Do not rewrite tags or reuse an
already published version to correct package metadata.

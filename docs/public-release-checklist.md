# Public release checklist

**Status:** preparation complete (2026-08-23). **The owner has decided to go
public**; what remains is execution, not deciding. Every preparation pull
request has merged (#60, #70–#75, #77, #78) and the source-distribution policy,
the pre-flip rescan and the refs/metadata audit below are done. What is left is
owner-only: the visibility switch itself, the repository settings in step 3, the
`pypi` environment, and the Trusted Publishers in step 5. PR #76 was closed —
see "Internal operational material" below.

**Purpose:** collect in one place everything that must happen at the moment of
the private → public switch, so that the *decision* stays separate from the
*execution*. Anything worth doing early is already done; only the steps that
are meaningful immediately before the flip are left under "Before the flip".

## Why this document exists

toolgraph is currently private. As a side effect **GitHub Actions bills paid
minutes**, so when account billing fails no CI job starts at all (from
2026-08-15: "The job was not started because recent account payments have
failed"). Public repositories on the same account (memtomem, memtomem-stm) keep
working because standard runners are free for them. Going public therefore
removes the CI cost problem automatically — but that is a *consequence* of the
decision, never a reason to make it. Settle the exposure items below first.

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

## Completed (2026-08-18)

- **No matches in the scoped secret scan.** Every blob across all 124 commits was scanned
  for `sk-ant-` / `ghp_` / `AKIA` / `AIza` / `xox*-` / `BEGIN * PRIVATE KEY`
  patterns: no matches. The only sensitive-looking filename ever committed is
  `.env.example`; no real `.env` exists. No local absolute paths (`/Users/…`),
  private IPs or internal hostnames appear in the documents.
  **Know this scan's limits:** it covers a short list of token patterns and
  filenames inside tracked Git objects only. That bounds both *where* it looked
  and *what* it could detect — it is not evidence that no secret of any shape
  exists.
- **Credential-shaped strings are all test sentinels.** `alice:secret@example.test`,
  `hunter2` and similar exist to verify the redaction contract. Deleting them
  would remove the regression coverage that prevents credential leaks.
- **Private-repository hard links removed.** Three canonical-source links and one
  companion-PR link were turned into plain text. They would have 404ed for a
  public reader while disclosing internal document paths and PR numbers.
  Sibling project **names** (syncmill / tracegraph) stay: they are essential to
  the architecture description, and naming a sibling project is not itself a
  disclosure.
- **Local development password cleaned up.** `docker-compose.yml` now follows
  `NEO4J_PASSWORD` and is labelled "not a secret". This also fixed a real bug:
  compose previously hardcoded the password, so changing `.env` desynchronised
  the database and the client and authentication failed.
- **New commits no longer carry a personal email.** The repository-local
  `git config` is set to the account's noreply address. Global config is
  untouched, so other repositories are unaffected.
- **Fork PR paths reviewed.** Releases run only on tag pushes, and PyPI uses
  OIDC (`id-token: write`) with the `pypi` environment rather than a hardcoded
  token. **Corrected 2026-08-23:** the CLA workflow (PR #70) introduces the
  repository's first `pull_request_target` and its first use of
  `PERSONAL_ACCESS_TOKEN`. It is safe because it checks out
  `ref: default_branch` with `persist-credentials: false` and never runs
  PR-branch code — the same pattern memtomem runs in production. Anyone editing
  that workflow must preserve that property.
- **Licensing alignment — merged (PRs #70, #71).** `LICENSE` is already
  byte-identical to memtomem's, including the trailing
  `Copyright 2025-2026 DAPADA Inc. and memtomem contributors`, and
  `pyproject.toml` uses the same `authors` form. Added on top: `CLA.md`
  (ASF ICLA plus a section-4 future-licensing grant),
  `.github/workflows/cla.yml`, `.github/cla-check.py` (copied verbatim) and
  `CONTRIBUTING.md`. Signatures are per repository, stored at
  `signatures/v1/cla.json` on a `cla-signatures` branch. No `NOTICE` file:
  memtomem carries none and Apache-2.0 does not require one — note this is a
  parity argument, not a legal one, and NOTICE obligations depend on what is
  actually redistributed. `contracts/tracegraph/` states its own copyright and
  license because it ships in the source distribution.

## Decided, not executed

- **History email rewrite — decided against (2026-08-23).** Two personal
  addresses appear as author and committer on existing commits. The reason for
  leaving them is the sibling standard: **memtomem is already public with the
  same contributor identities in its history, never rewritten, under an
  identical copyright line.** DAPADA has already made this call in public.
  A rewrite also cannot fully deliver what it promises — a force-push leaves the
  old commits reachable through `refs/pull/*/head` and direct SHA URLs, and
  GitHub Support declines cleanup for data that is not credential-grade.
  That said, "cannot guarantee complete erasure" is not the same as "achieves
  nothing": a rewrite does remove the data from ordinary branches, tags and
  fresh clones. This is a deliberate trade, not a technical impossibility.
  **What was done instead:** new commits already use the noreply address, and
  the plaintext addresses and the runnable mailmap were removed from this file.
  If the policy ever changes, the mailmap must map **email only and preserve
  author names** — the original draft rewrote the names too, erasing attribution.

- **Internal operational material — kept; the move was retracted (2026-08-23).**
  PR #76 proposed moving six files describing the toolgraph / syncmill /
  tracegraph integration into the private `memtomem-docs` repository, on the
  grounds that they were confidential. **That reasoning was wrong** — see the
  section above: the content stays readable in history and at the tip of every
  other open branch, so the move privatized nothing. The material was then
  reviewed and accepted as **not confidential**, which removed the only reason
  to move it.

  PR #76 was therefore **closed rather than merged**. Merging it would have cost
  1,439 lines, two runnable integration scripts, eight tests and two
  release-hardening assertions, while orphaning `scripts/gate_e_mcp_stub.py`
  (its only caller was the script #76 deleted) — all for no privacy benefit. A
  copy remains in `memtomem-docs` (memtomem-docs#64) as a harmless backup. If
  this material later becomes genuinely obsolete, remove it through a
  deprecation PR that says so and handles the stub.

- **Documentation language.** English is the default for every document.
  Korean is allowed only as an explicitly paired translation of an English
  original — currently `docs/beginner-guide.md` +
  `docs/ko-beginner-guide.md`, both linked from the README. That pair is a
  deliberate exception to the rule, not an oversight.
  All three previously Korean-only documents are now English: this checklist,
  `docs/context-engineering-tool-selection-report.md` and
  `docs/ecosystem-integration-plan.md` (the last of which stays in this
  repository now that #76 is closed).

## Before the flip

1. **Rescan — re-run 2026-08-23, no findings.** All 151 commits reachable from
   any ref were scanned with the commands below (the earlier pass covered 124).
   The only hits are this file's own documentation of the patterns it searches
   for. No `.env`, key, certificate or secret-shaped filename has ever been
   added on any ref. Commit *messages* carry no local paths and no addresses
   beyond `contact@dapada.co.kr` (deliberate, in `SECURITY.md`),
   `support@github.com` (Dependabot sign-off) and assistant/bot noreply
   addresses. Commit *metadata* still carries the two personal addresses on 144
   of 302 author/committer entries, which is the accepted decision recorded
   above, not a new finding. Re-run before the flip if further commits land:

   ```bash
   git grep -I -n -E 'sk-ant-|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|BEGIN (RSA|OPENSSH|EC) PRIVATE KEY' $(git rev-list --all)
   git log --all --pretty=format: --name-only --diff-filter=A | sort -u | grep -iE '\.env$|secret|\.pem$|\.key$'
   ```

2. **Audit the GitHub surface outside Git.** Actions logs and artifacts become
   public at the flip and private forks are detached. `docs/releasing.md`
   already requires this step.

   - Artifacts: audited 2026-08-23. 25 of them, all `policy-bundle-gateway-evidence`,
     containing digests, SHAs and boolean summaries. No exposure.
   - Run logs: **sampled, not exhaustive.** Of 178 runs, 31 never started
     (`steps=0`, the billing failure) and have no logs. Of the 147 that
     executed, 42 (≈29%, 6.7 MB) were downloaded and searched in full, covering
     the executed failures, the oldest runs, the release-hardening branch, the
     sibling-integration branches, the smoke successes and Dependabot. Result:
     no credential patterns, no email-shaped strings at all, no local paths
     beyond hosted-runner homes, no internal hosts. `***` masking (3–36 per run)
     is GitHub correctly hiding tokens. `release.yml` has never run.
     **Not covered:** the other 105 executed runs, run titles, job summaries.
   - Issue and PR text: audited and cleaned 2026-08-23 across 19 issues and 56
     PRs. Personal emails, real names, `/Users/…` paths, third-party product
     claims and private-repository PR links were removed from bodies, titles and
     comments. **Caveat:** GitHub keeps comment *edit history* visible to anyone
     with read access, so those redactions are cosmetic until each sensitive
     revision is deleted individually in the web UI.
   - Refs and metadata: audited 2026-08-23. 16 remote branches, all named after
     their change (`fix/`, `feat/`, `chore/`, `docs/`, `dependabot/`); nothing
     in a branch name discloses more than the commits already do. 151 commit
     messages scanned — see step 1. Labels are the GitHub defaults plus
     `dependencies`, `github_actions`, `python:uv`, `public-release` and
     `polish`. No milestones, no project boards, no releases and no tags exist.
     Merged-PR leftover branches were deleted and `delete_branch_on_merge` was
     enabled, so the ref list stays this short.
   - Forks: 0. Watchers: 0.

3. **Repository settings.** Note first that **changing visibility disables push
   rulesets**, so there is an unprotected window straight after the flip —
   re-enable and verify them as the first action, not later. Then enable: secret scanning
   and push protection, Dependabot alerts, code scanning, **private
   vulnerability reporting** (`SECURITY.md` already promises this channel but it
   is a separate, currently unset repository setting), and branch protection on
   `main`. **Do not use matrix-derived check names** as required checks —
   `artifact-smoke (ubuntu-latest, 3.14)` and friends orphan themselves whenever
   the matrix changes and leave pull requests waiting forever. Use one stable
   aggregator job covering lint, tests, every artifact-smoke leg, minimum-MCP,
   the runtime and extras audits, and package (issue #67). Note branch
   protection **cannot be configured at all while the repository is private** on
   this plan (the API returns 403).

   Done since: the repository description, topics and homepage are set,
   `CODE_OF_CONDUCT.md` is in the tree, and the `cla-signatures` branch now
   exists carrying an empty `signatures/v1/cla.json`, which is what
   `.github/cla-check.py` writes signatures into. Discussions still has no
   category guidance or moderation note while the README links to it.

   Notes on the CLA gate that earlier drafts got wrong: the `needs-cla` label is
   never read or written by the workflow or the script, and
   `PERSONAL_ACCESS_TOKEN` is optional — `.github/cla-check.py` falls back to
   `GITHUB_TOKEN`. The `CLA.md` / `CONTRIBUTING.md` scope contradiction that
   blocked PR #70 was resolved before it merged.

4. **CI.** Standard runners are free once public, so jobs should run regardless
   of billing. Note what that first green run actually proves: the Windows and
   Python 3.13/3.14 matrix legs cover the **artifact quickstart only**
   (`ci.yml`'s `artifact-smoke`). The full Neo4j/testcontainers suite still runs
   on a single Ubuntu job.

5. **Release via `docs/releasing.md`.** Do not restate a shorter sequence here.
   In outline: pending Trusted Publishers on **both** TestPyPI and PyPI (a
   pending publisher does not reserve the name), the local release gate,
   `test-v<VERSION>` → TestPyPI, clean-install verification, then the production
   `v<VERSION>` tag from the same reviewed commit. `pyproject.toml` embeds the
   README as package metadata, so **README and CHANGELOG must already be final
   in the tagged commit** (issue #63). The `pypi` environment referenced by
   `release.yml` does not exist on the repository yet. The source distribution
   now has an explicit allowlist (`[tool.hatch.build.targets.sdist]` in
   `pyproject.toml`): the package, the metadata files PyPI renders, and the JSON
   contracts — 46 entries, down from 145, with tests, docs, examples, scripts,
   CI workflows, the lockfile and `docker-compose.yml` no longer shipped. A
   freshly built archive was inspected and rebuilds the wheel standalone, and
   the wheel is byte-for-byte unaffected. `.gitignore` is still present because
   hatchling always ships the VCS ignore file; an exclude entry for it has no
   effect. **Inspect the archive again in the release PR** rather than trusting
   this note.

## Execution

The visibility switch itself is hard to reverse and exposes the whole history,
so the repository owner executes it directly once the items above are closed.

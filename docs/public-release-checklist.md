# Public release checklist

**Status:** preparation in progress (2026-08-23). The visibility decision has
not been made. Open blockers are tracked as issues #63 and #67 and as pull
requests #70–#76.

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
   decision below, every blob ever committed stays readable — `git show
   <commit>:<path>` works for anyone. Removing something in a new commit only
   changes the branch tip and the next sdist.
2. **Open branches carry their own copies.** A file removed on one branch is
   still at the tip of every other open branch until those are merged or
   deleted.

Any claim that something was "made private" by deleting or moving it is false
unless history is rewritten. See "Internal operational material" below, which
was originally filed under exactly that mistaken assumption.

## Completed (2026-08-18)

- **No real secrets.** Every blob across all 124 commits of history was scanned
  for `sk-ant-` / `ghp_` / `AKIA` / `AIza` / `xox*-` / `BEGIN * PRIVATE KEY`
  patterns: no matches. The only sensitive-looking filename ever committed is
  `.env.example`; no real `.env` exists. No local absolute paths (`/Users/…`),
  private IPs or internal hostnames appear in the documents.
  **Know this scan's limits:** it covers token patterns and filenames inside Git
  objects only, so "no secrets" is a claim about *tracked files*.
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
- **Licensing aligned with memtomem (PRs #70, #71).** `LICENSE` is already
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

- **Internal operational material — moved for tidiness, not for privacy
  (corrected 2026-08-23).** Six files describing the toolgraph / syncmill /
  tracegraph integration were moved to the private `memtomem-docs` repository
  (PR #76 here, memtomem-docs#64 there):
  `docs/gate-e-p4-operational.md`, `docs/ecosystem-integration-plan.md`,
  `scripts/gate_e_operational.py`, `scripts/ecosystem_smoke.py`,
  `tests/test_gate_e_operational.py`,
  `examples/governance-syncmill-smoke.yaml`.
  This was originally justified as protecting confidential material. **That
  justification was wrong** and is retracted: under the no-rewrite decision the
  content stays readable in history and at the tip of every other open branch,
  so the move privatizes nothing. It was reviewed on 2026-08-23 and the material
  was accepted as non-confidential. The move stands only as decluttering of the
  public tree; it cost eight tests, which is worth revisiting.

## Before the flip

1. **Rescan.** Re-run the scans above; commits added since 2026-08-18 have not
   been covered.

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
   - Refs and metadata: **not yet audited.** Branch names, 138 commit messages,
     labels, milestones and project boards all become public.
   - Forks: 0. Watchers: 0.

3. **Repository settings.** Enable immediately after the flip: secret scanning
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

   Also still missing: the repository description still says "thin MVP", topics
   and homepage are empty, and Discussions was enabled without a code of
   conduct, moderation policy or category guidance while the README links to it.

   The CLA workflow additionally needs a `PERSONAL_ACCESS_TOKEN` secret, a
   `cla-signatures` branch and a `needs-cla` label. **None of these exist yet**,
   so the workflow fails on its first run as things stand.

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
   has no include/exclude policy and currently ships workflows, tests, scripts,
   docs and this checklist — define an explicit allowlist and inspect the final
   archive before publishing.

## Not decided

Whether to go public at all. It is hard to reverse and exposes the whole
history, so the repository owner decides and executes it directly.

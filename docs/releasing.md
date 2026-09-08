# Release runbook

Toolgraph uses GitHub Actions and PyPI Trusted Publishing. No long-lived PyPI
token belongs in repository or environment secrets.

`release.yml` runs as two jobs. `build` checks the tag, runs the audits, builds
and verifies the distributions, and holds no publishing permission. `publish`
holds `id-token: write`, does not check out the repository, installs nothing,
and only downloads what `build` produced and uploads it.

Be precise about what that buys. The publish job still runs code — the
artifact download and the PyPI publisher are actions, pinned by commit. The
claim is narrower: **no checked-out project code and no installed dependency
runs in the job that can publish as this project**. Its one shell step compares
the downloaded files against the digests `build` recorded, which proves nothing
tampered with them between the jobs; it is not an independent attestation of
the build, because the same job produced both the artifact and the digest. A
compromised build still publishes a compromised artifact — the split removes
the much larger surface of build scripts and transitive build dependencies
reaching the token.

`tests/test_release_hardening.py` pins the shape of that job: only the two
allowlisted, sha-pinned actions; exactly one shell step, before either
publisher, whose commands come from a small allowlist; no `${{ }}` inside it;
no `continue-on-error` and no `if:` at job or step level that could let a
failed check through; `needs: build`; `environment: pypi`; and no `id-token`
reachable through workflow-level `permissions`, including the `write-all`
shorthand. The rule behind all of it is that **nothing is interpolated into a
`run:` block**.
`${{ }}` inside `run:` is textual substitution, so a value a compromised build
controls — an artifact filename, for instance — would become shell code
executing inside the privileged job. Pass values through `env:`.

## One-time public-release setup

1. Review the complete Git history, issues, Actions artifacts, and repository
   settings for credentials or private URLs before changing visibility.
2. Make `memtomem/toolgraph` public, then re-enable the push rulesets the
   visibility change disables. Confirm the README, license and security
   reporting link render. Branch protection and required checks come *after*
   CI has reported successfully on the release candidate. Existing recent
   success counts: a visibility flip does not require a dummy commit. `docs/public-release-checklist.md` step 3 has the
   ordered sequence.
3. Before any tag, create the `pypi` GitHub environment with the repository
   owner as required reviewer, administrator bypass disabled, and selected
   **tag** patterns `test-v*` and `v*` (no deployment branches). For a sole
   maintainer, allow self-review so the owner can approve after comparing
   evidence; this is a manual publication checkpoint, not two-person review.
   Read the settings back. Do not rely on implicit environment creation.
   Required reviewers may only be available after the repository is public.
4. On TestPyPI, create a pending Trusted Publisher for repository
   `memtomem/toolgraph`, workflow `release.yml`, environment `pypi`, package
   `toolgraph`.
5. Create the equivalent pending publisher on PyPI. PyPI and TestPyPI are
   separate services and both require configuration.

## Release candidate

1. Keep `toolgraph/__init__.py::__version__` at `0.0.1` for the first public
   alpha (the package classifier and README mark Alpha; this is not `0.0.1a1`).
   Finalize PR #87 against current main, including the README and both beginner
   guides. Date the changelog when the release candidate is confirmed. Do not
   merge the PyPI-first instructions while publication prerequisites are unmet.
2. Run the full local release gate:

   ```bash
   uv lock --check
   uv sync --frozen --group dev --extra ladybug
   uv run ruff check .
   uv run pytest -q
   scripts/audit-dependencies.sh runtime
   scripts/audit-dependencies.sh extras
   scripts/audit-dependencies.sh dev
   uv build
   uv run twine check dist/*
   scripts/verify-artifacts.sh dist <VERSION>
   ```

   `verify-artifacts.sh` checks that `dist/` holds the two files that will be
   uploaded **under exactly the names they must have** — plus `dist/.gitignore`,
   which `uv build` writes and which is tolerated by name (it is not uploaded:
   the publisher takes the distributions, and hidden files are excluded from
   the artifact) — the release
   passes the version from the tag, so an unexpected artifact name is rejected
   before it travels into a later step — that the sdist carries only what the
   allowlist in `pyproject.toml` intends and nothing but plain files, and that
   the sdist rebuilds the wheel byte for byte.

   Read that last check for what it is: a same-runner comparison showing the
   published source archive is a faithful build input, which is exactly what an
   include allowlist can silently break. It is not a cross-machine
   reproducibility guarantee — the runner image, the Python version and the
   isolated build's transitive dependencies all still float. It does depend on
   the backend being pinned (`[build-system] requires`), so bump that pin
   deliberately and rehearse after doing so.

3. Merge only after required CI passes.
4. Push `test-v<VERSION>` first. The release workflow rebuilds and publishes
   to TestPyPI through OIDC. The rehearsal uses `skip-existing`, so it can be
   re-run; the production upload does not, and a version that already exists
   there fails the release instead of reporting success.
5. Verify **published** TestPyPI files against the candidate, not a successful
   workflow badge or a fresh `skip-existing` build:

   ```bash
   python scripts/verify_index_release.py --index testpypi --version 0.0.1 --dist dist
   ```

   In a new Python 3.12 environment outside the checkout, download only the
   exact Toolgraph wheel from TestPyPI, then install it with dependencies from
   PyPI. Do not use `--extra-index-url` to mix project selection across indexes:

   ```bash
   python -m pip download --no-deps --only-binary=:all: \
     --index-url https://test.pypi.org/simple/ --dest downloaded toolgraph==0.0.1
   # Compare the downloaded wheel SHA-256 with the verifier's output first.
   python -m pip install --index-url https://pypi.org/simple/ \
     'downloaded/toolgraph-0.0.1-py3-none-any.whl[ladybug]'
   python -m pip check
   toolgraph --version
   toolgraph example init quickstart
   cd quickstart
   toolgraph init
   toolgraph crawl --servers servers.yaml
   toolgraph ingest-manifest --governance governance.yaml --strict-drift
   toolgraph eligible-tools vibe-coder policy-gateway::read_note policy-gateway::publish_note --profile review
   toolgraph selection-explain vibe-coder policy-gateway::publish_note
   toolgraph policy compile --agent vibe-coder --profile review --output .toolgraph/policy-bundle.json
   ```

   Require `read_note` eligible and `publish_note` rejected, with graph-path
   evidence and a valid generated bundle. No sibling checkout, Docker, Node.js,
   provider credential or paid model call is required.
6. Push the immutable production tag `v<VERSION>` from the same reviewed
   commit. Confirm the PyPI metadata, wheel contents, provenance, and install.
   Both runs print `sha256sum dist/*`. Compare them — and be clear-eyed about
   when you can: the production digest does not exist until the production run
   is already under way, so this comparison **cannot block that publication**
   by itself. The required reviewer configured above makes it a gate: the build
   job finishes and prints its digest, and the publish
   job then waits for approval. Without that, the comparison is a post-hoc
   check and a mismatch means yanking, not preventing. Note also that a re-run of the rehearsal prints a digest
   for a freshly built file that `skip-existing` may not have uploaded, so
   compare against the digest from the run that actually uploaded. Same commit
   is not by itself the same artifact, which is why the toolchain is pinned in
   `release.yml` and in `[build-system]`.

Do not reuse a tag after a failed release. Fix forward with a new prerelease or
patch version. Repository visibility and package publication are external state
changes and require an explicit release decision by a maintainer.

## Promotion and completion record

While the production publish job waits for environment approval, download its
`dist` artifact into a new directory and run `verify_index_release.py --index
testpypi --version 0.0.1 --dist <production-dist>`. A mismatch, missing file,
yanked release or unavailable index blocks approval. Only the two expected
filenames are accepted. Retain the source SHA, both tag targets, workflow run
IDs and both published file digests in the release evidence.

After production upload, run the verifier with `--index pypi`, repeat the clean
installation and quickstart from PyPI, and check metadata and publishing
attestations. Then create the GitHub Release titled `v0.0.1 — Alpha` with its
prerelease flag enabled to communicate maturity; the Python package version
remains `0.0.1`. The release notes must state the advisory boundary and link to
the standalone quickstart. Do not call a successful local build publication.

For a defective published release, yank it and fix forward with a new version;
do not delete/recreate release tags or overwrite index files. A failed network
lookup is unknown state: read back index files before deciding how to recover.

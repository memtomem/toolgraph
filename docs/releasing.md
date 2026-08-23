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
   CI has run successfully at least once — a check cannot be marked required
   until it has reported. `docs/public-release-checklist.md` step 3 has the
   ordered sequence.
3. On TestPyPI, create a pending Trusted Publisher for repository
   `memtomem/toolgraph`, workflow `release.yml`, environment `pypi`, package
   `toolgraph`.
4. Create the equivalent pending publisher on PyPI. PyPI and TestPyPI are
   separate services and both require configuration.

## Release candidate

1. Set `toolgraph/__init__.py::__version__` and move the changelog entry from
   `Unreleased` to the release date.
2. Run the full local release gate:

   ```bash
   uv lock --check
   uv sync --frozen --group dev --extra ladybug
   uv run ruff check .
   uv run pytest -q
   scripts/audit-dependencies.sh runtime
   scripts/audit-dependencies.sh extras
   uv build
   uv run twine check dist/*
   scripts/verify-artifacts.sh dist <VERSION>
   ```

   `verify-artifacts.sh` checks that `dist/` holds the two files that will be
   uploaded **under exactly the names they must have** — plus `dist/.gitignore`,
   which `uv build` writes and which is tolerated by name (it is not uploaded:
   the publisher takes the distributions, and hidden files are excluded from
   the artifact) — — the release
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
5. Install the TestPyPI artifact into a clean Python 3.12 environment and run
   the complete `toolgraph example init` quickstart. TestPyPI may need PyPI as
   an additional dependency index because it does not mirror dependencies.
6. Push the immutable production tag `v<VERSION>` from the same reviewed
   commit. Confirm the PyPI metadata, wheel contents, provenance, and install.
   Both runs print `sha256sum dist/*`. Compare them — and be clear-eyed about
   when you can: the production digest does not exist until the production run
   is already under way, so this comparison **cannot block that publication**
   by itself. To make it a gate, add a required reviewer to the `pypi`
   environment: the build job finishes and prints its digest, and the publish
   job then waits for approval. Without that, the comparison is a post-hoc
   check and a mismatch means yanking, not preventing. Note also that a re-run of the rehearsal prints a digest
   for a freshly built file that `skip-existing` may not have uploaded, so
   compare against the digest from the run that actually uploaded. Same commit
   is not by itself the same artifact, which is why the toolchain is pinned in
   `release.yml` and in `[build-system]`.

Do not reuse a tag after a failed release. Fix forward with a new prerelease or
patch version. Repository visibility and package publication are external state
changes and require an explicit release decision by a maintainer.

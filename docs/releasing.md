# Release runbook

Toolgraph uses GitHub Actions and PyPI Trusted Publishing. No long-lived PyPI
token belongs in repository or environment secrets.

## One-time public-release setup

1. Review the complete Git history, issues, Actions artifacts, and repository
   settings for credentials or private URLs before changing visibility.
2. Make `memtomem/toolgraph` public and confirm the README, license, security
   reporting link, branch protection, and required CI checks render correctly.
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
   ```

3. Merge only after required CI passes.
4. Push `test-v<VERSION>` first. The release workflow rebuilds and publishes
   to TestPyPI through OIDC.
5. Install the TestPyPI artifact into a clean Python 3.12 environment and run
   the complete `toolgraph example init` quickstart. TestPyPI may need PyPI as
   an additional dependency index because it does not mirror dependencies.
6. Push the immutable production tag `v<VERSION>` from the same reviewed
   commit. Confirm the PyPI metadata, wheel contents, provenance, and install.

Do not reuse a tag after a failed release. Fix forward with a new prerelease or
patch version. Repository visibility and package publication are external state
changes and require an explicit release decision by a maintainer.

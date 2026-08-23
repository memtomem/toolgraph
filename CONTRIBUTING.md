# Contributing to Toolgraph

Thank you for your interest in contributing to Toolgraph!

Toolgraph is an advisory analyzer: it explains reachability and impact over a
tool/policy graph. It does **not** sit in the traffic path and does not block
calls at runtime. Changes that would move it toward enforcement are out of
scope — see the "Advisory, not enforcement" note in [README.md](README.md).

## Development setup

```bash
git clone https://github.com/memtomem/toolgraph.git
cd toolgraph

# Requires Python 3.12+ and uv
uv sync --group dev --extra ladybug

# Lint (same command CI runs)
uv run ruff check .

# Tests — these use a real Neo4j through testcontainers, so a running
# Docker daemon is required. There is no Cypher mocking.
uv run pytest
```

`ruff` is deliberately held below 0.16: the repository has no `[tool.ruff]`
configuration yet, and 0.16's changed defaults surface a large batch of
unrelated findings. Pin removal is tracked separately — please do not bump it
as a drive-by change.

`make install` intentionally omits the `ladybug` extra, so the Ladybug-backed
tests `importorskip` and are silently skipped. Use the `uv sync` command above
if you are touching either backend.

## Pull requests

- Branch from `main`; use a short prefixed name (`fix/`, `feat/`, `chore/`,
  `docs/`).
- Reference the issue you are closing in the PR body (`Closes #N`).
- Keep the advisory safety boundary explicit in the description when your
  change touches contracts, policy evaluation, or the CLI surface.
- Required CI must pass before merge.

Releases follow [docs/releasing.md](docs/releasing.md); contributors do not
need to run that process.

## Contributor License Agreement (CLA)

Before we can merge your first pull request, you need to sign the
[Contributor License Agreement](CLA.md). The CLA workflow will automatically
comment on your PR with instructions — you sign by replying with:

> I have read the CLA Document and I hereby sign the CLA

You only need to sign once per GitHub account per repository. Because Toolgraph
and [memtomem](https://github.com/memtomem/memtomem) are separate repositories
with independent signature stores, contributors who open pull requests against
both projects need to sign in each repository (still one-time per account).
Your signature is stored in `signatures/v1/cla.json` on the `cla-signatures`
branch of whichever repository you signed.

The CLA is adapted from the Apache Software Foundation Individual Contributor
License Agreement with one additional section covering future licensing rights.
This preserves DAPADA Inc.'s ability to adopt different license terms for the
Work in the future (for example, a dual-licensing arrangement) without needing
to re-collect consent from every contributor. The CLA does not change the
current license of the Work, which remains Apache License 2.0.

For questions about the CLA, contact contact@dapada.co.kr.

## Reporting issues

Security vulnerabilities go through the private channel described in
[SECURITY.md](SECURITY.md) — please do not open a public issue for them.

For everything else, include the Toolgraph version, the graph backend you are
using (`neo4j` or `ladybug`), your OS, and your Python version. The two
backends fail in different ways and that detail is usually what makes a report
reproducible.

# Vendored Tracegraph review-candidate contract

Toolgraph G3 consumes the repository-vendored Tracegraph T3 contract rather
than depending on the Tracegraph wheel. These files come from a merged
contract change in `memtomem/tracegraph` (a private companion repository),
PR #10, merge commit `822272e6c5c88dd1c1a2319a27d6513484eb1de8`. Those
coordinates are not resolvable from outside the organization; they are
recorded so a maintainer can trace the refresh:

- `contracts/review-candidates.schema.json`
- `tests/fixtures/review-candidates/v1.json`

The golden was produced from Tracegraph's deterministic `source.json`; its
`artifact_digest` is the SHA-256 of the exact normalized artifact queried.
Update this directory only from a merged producer contract and re-run the
Toolgraph contract suite after any refresh.

## Provenance and license

Tracegraph (`memtomem/tracegraph`) and Toolgraph have the same owner, so this
directory is not third-party material. These files are

    Copyright 2025-2026 DAPADA Inc. and memtomem contributors

and are licensed under the Apache License, Version 2.0 — the same terms as the
rest of this repository. See [`LICENSE`](../../LICENSE).

This paragraph exists because the directory ships in the source distribution:
`[tool.hatch.build.targets.wheel] packages = ["toolgraph"]` keeps it out of the
wheel, but hatchling's sdist includes tracked files by default. Vendored
material that is redistributed needs its grant stated where it sits, not
inferred from the repository root.

Do **not** add material from a repository with a different owner here without
recording its own license and grant.

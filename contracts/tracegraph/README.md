# Vendored Tracegraph review-candidate contract

Toolgraph G3 consumes the repository-vendored Tracegraph T3 contract rather
than depending on the Tracegraph wheel. These files come from merged
Tracegraph PR #10, merge commit `822272e6c5c88dd1c1a2319a27d6513484eb1de8`:

- `contracts/review-candidates.schema.json`
- `tests/fixtures/review-candidates/v1.json`

The golden was produced from Tracegraph's deterministic `source.json`; its
`artifact_digest` is the SHA-256 of the exact normalized artifact queried.
Update this directory only from a merged producer contract and re-run the
Toolgraph contract suite after any refresh.

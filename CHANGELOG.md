# Changelog

All notable changes to Toolgraph are documented here.

## Unreleased

### Changed

- **Breaking:** requires the MCP SDK 2.x (`mcp>=2.1.1,<3`). Environments pinned
  to `mcp` 1.x can no longer install this package, because 2.x removed
  `mcp.server.fastmcp` and renamed every model field from camelCase to
  snake_case. The MCP wire format is unchanged, so existing clients need no
  change.
- Unexpected exceptions raised inside an MCP tool no longer put their message
  on the wire. Only errors toolgraph raises deliberately as `ContractError`
  (for example an unknown selector profile) are forwarded; everything else
  keeps the SDK's generic text, so an internal failure cannot disclose
  filesystem paths, query bodies or credential-shaped values to a caller.

## 0.0.1 - 2026-09-09

Initial public alpha release.

- Crawl MCP servers into a graph and combine factual capability data with
  operator-authored governance.
- Explain access, reachability, drift, blast radius, and tool selection.
- Compile deterministic policy bundles for an independent runtime gateway.
- Support local Ladybug and shared Neo4j backends.
- Reject unknown manifest fields and duplicate YAML keys before graph mutation.
- Persist only credential-free endpoint labels and redact failure diagnostics.
- Ship an install-only, offline quickstart through `toolgraph example init`.
- Give MCP consumers a typed `backend_unavailable` error envelope for transient
  graph outages while keeping contract and internal errors fail-loud. Outages
  are typed at the backend seam, and `retryable` follows the tool's advertised
  read-only annotation instead of being assumed, failing closed to `false`
  whenever that annotation cannot be read.
- Add a body-free `toolgraph.control-plan` v1 contract and advisory
  `control-preflight` command that lints bounded DAG topology and evaluates all
  principal/tool surfaces under one collision-safe graph-state bracket.

### Stabilization (2026-09-08)

- Resolve ingest references in bounded batches shared with the selector,
  preserving atomic application, diagnostic order and ambiguous identities.
- Require explicit commit SHAs for ecosystem smokes and clean matching HEADs
  for gateway smokes; record dependencies and artifact evidence.
- Include Ladybug in `make install` and refresh the pip audit dependency lock.
- Reconcile historical integration and selection roadmaps with shipped code.

### Implementation review fixes (2026-09-07)

- Handle empty crawl and manifest collections on both backends while retaining
  stale-edge reconciliation, revocation, and transactional generation updates.
- Preserve opaque artifact identities, including ordinary URI queries/fragments.
  Reject credential-shaped identities and redact only evidence fields, including
  policy review paths. Existing v1 schemas and decision vocabularies are unchanged.
- Use collision-safe `v2:` exception keys and deterministic last-entry-wins
  metadata for duplicate logical exceptions. No physical schema migration is
  required: existing keys remain readable. Re-ingest the original governance
  manifest to rebuild keys and restore entries lost to old key collisions, then
  recompile policy bundles. Old artifacts cannot reconstruct lost manifest rows.
- Require control-plan fields exactly as published: missing `maximum_cycles`,
  explicit null bounds, string/boolean coercions are rejected. JSON integer
  numbers such as `2.0` remain accepted; additive same-major fields remain valid.
- Compile catalogs in chunks of at most 4,096 candidates under one strict state
  bracket; reject incomplete or conflicting evaluations. Injected readers own
  all state reads, and adapters without the optional `evaluate` capability work.
- Load runtime settings lazily so help/version and explicit configuration remain
  usable when the default config is broken. Invalid selected configs fail clearly.
- Reject naive and sub-minute timestamps before preflight/review I/O; development
  validation now installs and checks the RFC 3339 format validator.

### Upgrade note for pre-release users (historical)

`driver.session()` and `driver.verify_connectivity()` now raise
`BackendUnavailableError` where they previously surfaced the raw `neo4j`
`ServiceUnavailable`, `SessionExpired`, `ConnectionAcquisitionTimeoutError`, or
`DatabaseUnavailable`. Code embedding toolgraph as a library and catching those
types directly must catch `BackendUnavailableError` (or its `BackendLockedError`
subclass); the original exception is preserved as `__cause__`. Configuration,
authentication, and Cypher/client errors are unaffected, as is `get_driver()`.
Successful CLI output and query result shapes are unchanged; an unhandled
outage now names `BackendUnavailableError` in the traceback.

`driver.is_backend_unavailable()` narrowed to match: it now recognizes only
`BackendUnavailableError` (anywhere in the cause chain), not the raw `neo4j`
types it previously classified. Code that pairs the still-raw `get_driver()`
escape hatch with `is_backend_unavailable()` to decide whether to retry will
silently stop matching — catch `BackendUnavailableError`, or route the call
through `driver.session()` so the seam types it.

Earlier snapshots could persist stdio arguments or complete HTTP URLs in graph
endpoint/evidence properties. Reset and rebuild pre-release graphs after
upgrading. If any argument or URL contained a credential, rotate it as well.

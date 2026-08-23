# Changelog

All notable changes to Toolgraph are documented here.

## 0.0.1 - Unreleased

First public release. The `v0.0.1` tag, and with it the PyPI publication,
follows the repository's visibility switch; see `docs/releasing.md`.

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

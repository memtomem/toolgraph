# Changelog

All notable changes to Toolgraph are documented here.

## 0.1.0 - Unreleased

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
  read-only annotation instead of being assumed.

### Upgrade note for pre-release users

`driver.session()` and `driver.verify_connectivity()` now raise
`BackendUnavailableError` where they previously surfaced the raw `neo4j`
`ServiceUnavailable`, `SessionExpired`, `ConnectionAcquisitionTimeoutError`, or
`DatabaseUnavailable`. Code embedding toolgraph as a library and catching those
types directly must catch `BackendUnavailableError` (or its `BackendLockedError`
subclass); the original exception is preserved as `__cause__`. Configuration,
authentication, and Cypher/client errors are unaffected, as is `get_driver()`.
Successful CLI output and query result shapes are unchanged; an unhandled
outage now names `BackendUnavailableError` in the traceback.

Earlier snapshots could persist stdio arguments or complete HTTP URLs in graph
endpoint/evidence properties. Reset and rebuild pre-release graphs after
upgrading. If any argument or URL contained a credential, rotate it as well.

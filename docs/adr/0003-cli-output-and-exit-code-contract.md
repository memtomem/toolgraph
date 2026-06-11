# 0003 — CLI contract: stdout is machine output, exit codes are opt-in gates

Status: Accepted · Date: 2026-06-11

## Context

Query commands print JSON to stdout, but diagnostics (crawl progress, ingest
REJECTED lines, `unsafe-tools` AGENT_NOT_FOUND text) also go to stdout,
polluting piped output. And `check-access` exits 0 on a DENY verdict — an
advisory analyzer's natural adoption path is CI gating ("fail the build when
a new violation appears"), which an always-0 exit blocks.

External review pushed one refinement: don't change human-facing defaults;
gating must be opt-in.

## Decision

1. **stdout/stderr split**: commands that emit JSON reserve stdout for that
   JSON; all progress, warnings, notices, and error text go to stderr.
2. **Opt-in gate flags**, no change to default exit behavior:
   - `check-access --fail-on-deny` → exit 1 when `verdict == "DENY"` and
     `all_authorized` is false; exit 0 on authorized-only DENY.
   - `unsafe-tools --fail-on-violation` → exit 1 when any `violation` row
     remains after filtering.
   - Not-found verdicts keep their existing nonzero exits.
3. The exit-code table is documented in the README; it is a contract, not an
   implementation detail.

## Consequences

- `toolgraph unsafe-tools bot | jq` works without sed-stripping noise.
- CI can gate on governance without parsing JSON.
- Scripts that depended on diagnostics appearing on stdout must switch to
  stderr (accepted breakage; pre-1.0).

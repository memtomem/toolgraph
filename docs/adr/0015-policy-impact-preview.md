# 0015 — Read-only semantic policy impact preview

Status: Proposed · Date: 2026-10-16

Tracking: [issue #158](https://github.com/memtomem/toolgraph/issues/158).

## Context

Structural ingest previews do not show which selector decisions change. A
preview must agree with real ingest, including last-entry-wins metadata,
exceptions, drift, and removal of authored-only tools, without temporarily
changing a live graph.

## Proposed decision

Read current facts and validate the entire candidate manifest in one strict
graph-state bracket through a read-only session. Normalize resolved manifest
edges with the same helper as ingest. Evaluate replacement governance in
memory, sharing selector feature assembly, filtering, risk scoring, and deny
evidence formatting. Never apply-and-rollback on the source database.

The initial scope is one agent and 1–4096 explicitly selected tool references.
A projection attempt is limited to 100,000 rows and 50 MB of serialized data;
limits reject the whole attempt. These are retained-data limits, not bounds
on database-driver buffering or server-side query work.

`policy impact` emits Markdown or a versioned `toolgraph.policy-impact` JSON
artifact. It binds both governance digests, graph instance/generation, profile,
selected references, and their catalog fingerprint. It separates eligibility
changes from unresolved identity and evidence-only changes. Credentials are
refused in identities and scrubbed in evidence; display redaction never
rewrites executable identities.

`policy check` uses the proposed-side decisions in that artifact as explicit
regression expectations. It permits governance/generation changes but requires
the same instance and scoped catalog fingerprint. Unresolved comparisons are
not executable expectations. Checking never applies policy or attests gateway
enforcement; the artifact is an operator-controlled expectation, not a signed
authorization grant.

## Consequences

Both backends need parity tests comparing hypothetical results with actual
ingest in disposable databases. Existing selector and dry-run wire contracts
remain unchanged. Invalid manifests, absent provenance, unstable reads, and
capacity failures cannot become successful partial comparisons. A reset or
catalog change requires generating and reviewing a new expectation artifact.

This proposal is implemented for review without promoting the ADR to Accepted.
Whole-organization analysis, automatic application, hosted UI and notification
workflows are outside this first version.

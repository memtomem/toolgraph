# 0014 — Require an explicit empty fleet declaration

Status: Proposed · Date: 2026-10-16

Tracking: [issue #154](https://github.com/memtomem/toolgraph/issues/154).

## Context

ADR-0007 treats an empty servers file as an empty fleet. At review baseline
`1d543b94ec62fbc3821f59a05870c1e042bf1586`, `load_yaml_mapping` maps a blank
or comment-only document to `{}`, and `ServersConfig.servers` defaults to
`[]`. Consequently, blank files, comment-only files, `{}`, and `servers: []`
all cause `crawl` with its default pruning behavior to retire every server.

An isolated Ladybug 0.18.3 reproduction confirmed that all four forms exit
successfully, remove the server and its EXPOSES edges, and increment graph
generation. Authored governance and its DENY verdict survive, as ADR-0007
requires. This is an intentional existing contract, not a newly discovered
authorization bypass. It nevertheless makes accidental empty configuration
indistinguishable from an operator's explicit fleet retirement request.

## Proposed decision

1. Require the `servers` key in `ServersConfig`. Reject blank files,
   comment-only files, and mappings without that key. Reject `servers: null`
   and other non-list values using the existing strict model validation.
2. Keep `servers: []` as the explicit declaration of an empty fleet. With
   default pruning it retires all crawled servers; with `--no-prune` it
   preserves them. No new confirmation prompt or CLI flag is introduced.
3. Validate before opening MCP connections, initializing schema, loading
   results, or pruning. Invalid configuration produces the existing invalid
   servers-config diagnostic on stderr and exit code 1. Graph contents,
   generation, and instance identity remain unchanged.
4. Apply the same required-key contract to library configuration parsing.
   Do not change the generic YAML loader or governance-manifest empty-input
   semantics as part of this decision.
5. Preserve all ADR-0007 retirement rules: authored governance survives,
   retained tools lose stale exposure and annotation claims, and a retirement
   remains atomic with its generation update.

## Compatibility and migration

This intentionally changes ADR-0007's consequence that an empty file declares
an empty fleet. Operators and library callers using a missing key to mean
"retire everything" must supply `servers: []` or `ServersConfig(servers=[])`.
Publish that migration in the changelog and crawl documentation when the
implementation lands. Existing explicit lists keep their meaning.

ADR-0007 remains Accepted while this record is Proposed. Acceptance of this
record would amend only its empty-file interpretation, not supersede its
fleet reconciliation design. This proposal does not change current runtime
behavior.

## Alternatives

- Keep the current behavior and add documentation: compatible, but an
  accidentally emptied file still requests destructive reconciliation.
- Require a confirmation or a new allow-empty flag: stronger friction, but
  it complicates unattended declarative workflows. An explicit empty list
  provides intent without another CLI mode.

## Acceptance criteria for implementation

- Blank, whitespace-only, comment-only, `{}`, missing-key, null-list, and
  wrong-type inputs fail before any connection or graph mutation.
- On both supported backends, rejected input preserves graph counts,
  instance ID, generation, and authored governance.
- `servers: []` retires crawl-owned state under default pruning, preserves
  governed tools/resources and DENY evidence, and leaves those tools drifted.
- `servers: []` with `--no-prune` preserves the existing fleet. Repeating an
  already-empty retirement does not bump generation.
- Existing named/unnamed failure handling and nonempty fleet reconciliation
  continue to pass. CLI help, guides, and migration notes agree with the new
  contract.

## Review evidence

See [the implementation review](../reviews/2026-10-16-performance-security-usability.md)
for the baseline, measured behavior, tracking issues, and validation limits.

# 0007 — Fleet-level crawl reconciliation: servers.yaml declares the fleet

Status: Accepted · Date: 2026-06-11

## Context

Loader reconciliation is per-server and only fires when that server is
crawled. A server removed from `servers.yaml` (or renamed — removal plus a
new server) is never crawled again, so its entire crawled subgraph survives
indefinitely (issue #25, found by Codex adversarial review of the #14/#15
stack):

- the stale `MCPServer` node keeps its EXPOSES edges, so the old tools are
  not even `drifted` and pass the `is_drifted` checks in the selector and
  `eligible-tools`;
- stale annotation hints keep feeding `destructive-unsafeguarded` /
  `annotation-contradictions` with self-claims nobody is making;
- stale bare-name twins make every bare ref `AMBIGUOUS_TOOL` after a rename;
- stale rows pollute `unmapped-tools`, `unsafe-tools`, `blast-radius`, …

ADR-0001 made node existence a truth signal, but nothing retired nodes whose
*server* went away.

## Decision

1. `servers.yaml` declares the fleet, the same way the governance manifest
   declares authored truth. After loading a crawl pass, `toolgraph crawl`
   retires every `MCPServer` node whose name is not accounted for
   (`--no-prune` opts out, for subset files that do not declare the whole
   fleet).
2. Accounted for = successfully crawled this pass, OR listed in
   `servers.yaml` with a `name:` override. The override is the only identity
   that survives a crawl *failure* — node names come from `serverInfo.name`,
   which a failed crawl never saw. **A failed spec without a `name:` override
   therefore skips the whole prune** (with a warning telling the operator to
   name their servers): a server that failed to crawl is not a server that
   was removed, and guessing identities (e.g. by endpoint match) risks
   pruning a live-but-unreachable server.
3. Retirement follows the existing per-server keep-rule, one level up:
   - annotation hints on the server's tools are cleared (ADR-0006: a dead
     server makes no claims);
   - tools without authored governance are deleted; tools WITH authored
     `CAN_CALL`/`READS`/`WRITES`/`GOVERNED_BY` are kept — losing their
     EXPOSES makes them honestly `drifted` — and surfaced as warnings;
   - resources follow the ADR-0001 degree-0 rule: no PROVIDES from anyone
     and no authored edge → deleted; shared or governed resources stay;
   - the `MCPServer` node itself is deleted.
4. The whole prune is one transaction; the generation bumps only when
   something was actually retired (ADR-0004: a no-op must not invalidate
   caches).
5. Identity is load-bearing, so it is carried, not inferred: crawl failures
   return their `ServerSpec` (whether a failed spec was named must not be
   guessed from a display label — an unnamed spec's command can collide
   with another spec's name), and a crawl pass where two servers claim the
   same graph name is rejected before any load (two specs MERGEing into
   one node would last-load-wins reconcile each other's tools away, prune
   or no prune).
6. Manifest ingest deletes degree-0 `Tool` nodes, mirroring the ADR-0001
   resource rule. A tool kept only for its authored governance loses its
   last edge when the manifest stops mentioning it, and no crawl will ever
   reconcile a retired server's name again — without this, the documented
   exit path ("remove it from the manifest and re-ingest") would leave a
   ghost twin in bare-name resolution forever. Live crawled tools always
   carry EXPOSES and never match.

## Consequences

- Stale EXPOSES can no longer defeat the drift checks; renames stop
  producing permanent `AMBIGUOUS_TOOL` twins (the governed remainder is
  visibly drifted instead of silently live).
- Everything the prune deletes is crawl-owned and reconstructible by
  re-crawling; authored governance is never deleted. The worst case of an
  accidental prune (wrong/partial servers.yaml without `--no-prune`) is a
  re-crawl with the right file.
- An empty `servers.yaml` declares an empty fleet and retires every crawled
  server — consistent with the declarative model, same as an empty manifest
  pruning all agents/policies.
- Fleets that name their servers in `servers.yaml` get reconciliation that
  keeps working through crawl failures; unnamed fleets lose pruning for the
  duration of any failure. Naming servers is now the documented best
  practice.

# 0018 — Authored resource containment with inherited DENY

Status: Proposed · Date: 2026-11-18

Tracking: [issue #299](https://github.com/memtomem/toolgraph/issues/299).

## Context

A policy attaches to a tool or to one exact resource. A generic tool such as
`filesystem::write_file` takes its target as an argument, so an operator can
either govern the whole tool or author one `data_access` edge to one URI and
leave every other target unconstrained. #299 names two gaps:

1. Nothing binds a tool parameter to the resources its values name.
2. Resources have no hierarchy. The relationship types are EXPOSES, PROVIDES,
   CAN_CALL, READS, WRITES and GOVERNED_BY, and every resource match is by
   identity. "Everything under `data` is DENY" cannot be stated.

This ADR closes the second gap only. The first needs a consumer contract
(see "Deferred"), and the boundary keeps call arguments out of Toolgraph.
ADR-0013: "Prompts, work products, commands, credentials, and repository paths
are neither necessary nor appropriate control-plane inputs." ADR-0010:
"memtomem-stm can enforce list and call decisions without a database or
Toolgraph process on the call hot path."

#299 proposed governance on a URI prefix. That would make URI normalization
decide DENY, and Toolgraph has no written normalization rule:
`normalize_resource_uri` (`toolgraph/uris.py:28`) is `str(AnyUrl(uri))`.
Measured at `9255fd8` with pydantic's `AnyUrl`:

| Input | Stored as |
|-------|-----------|
| `file:///Data/X/` | `file:///Data/X/` (path case and trailing `/` kept) |
| `http://Example.COM/X` | `http://example.com/X` |
| `demo://Res/a` | `demo://Res/a` (host case kept outside special schemes) |
| `demo://res/a/../b` | `demo://res/b` |
| `memory:Foo/../bar` | `memory:Foo/../bar` (opaque: `..` kept) |
| `file:///a/%2F`, `file:///a/%2f` | unchanged, two different strings |
| `file:///café` | `file:///caf%C3%A9` |

A string prefix also needs a segment rule: `file:///data` is a prefix of
`file:///database`. The URI identity tripwire (#11) and the shape-routed
`governed_by` false ALLOW (#280) were both silent ALLOWs from identity
mismatches; a prefix rule widens that surface.

The Relate contest entry prototypes argument checks outside the repository
with Toolgraph 0.4.1 unmodified. As of its plan of 2026-11-18 (kept outside
this repository), a review of that plan rejected path arguments after four
major findings, all from paths: a symlink swapped between check and use, case
and NFC/NFD forms, DENY descendants of a recursive operation, and parameters
left out of the binding. The plan moved to resource IDs and explicit
containment. The prototype was not built at that date.

## Proposed decision

1. **Containment is authored.** A new manifest section declares
   `{parent, child, provenance}` pairs, ingested as
   `Resource-[:CONTAINS]->Resource`.
   - Crawl never creates it (as ADR-0006 rules for annotations), and crawl
     retirement keeps it like every other authored edge (ADR-0007).
   - The hierarchy is a tree: ingest rejects a second parent, a cycle, and a
     chain deeper than a constant `N`. Every query uses the same `N`, so a
     manifest that passes ingest has no DENY ancestor out of a query's reach.
   - CONTAINS is replaced on every ingest like the other authored edges
     (`toolgraph/manifest/ingest.py:485`). Per ADR-0001, the same PR makes a
     CONTAINS edge count as authored for the resource prune.
   - Containment endpoints join the resource set the #280 collision check
     reads (`manifest_resources`, `toolgraph/manifest/ingest.py:253`, today
     only `data_access` and exceptions). A `governed_by` key that names both a
     tool and a contained resource is then rejected, not routed as a tool.
   - Both backends store it; on Ladybug it is a new relationship table, so
     `BACKEND_SCHEMA_VERSION` (`toolgraph/graph/schema.py:28`) is bumped.
2. **Both endpoints pass an identity rule at ingest,** and a failing URI is
   rejected, not rewritten:
   - it is a fixed point of `normalize_resource_uri`;
   - a value with a scheme must parse as `AnyUrl`. `normalize_resource_uri`
     returns an unparseable value unchanged and warns only when it contains
     `://`, so `https:` fails to parse yet is a silent fixed point today
     (measured);
   - it contains no whitespace, control character, quote, `?`, `#` or `@`.
     Parsing does not cover these: `AnyUrl` accepts `memory:foo bar` and
     `memory:a"b` unchanged (measured);
   - `is_credential_shaped_identity` (`toolgraph/redaction.py:196`) does not
     refuse it. The character rule alone misses `memory:token=SECRET` and
     `file:///data/%3Ftoken%3DSECRET` (measured).

   The rule also excludes legitimate crawled identities such as
   `https://example.test/items?page=2` or `memory:item#section`. They cannot
   take part in containment; they keep their exact identity and can still be
   governed directly or through their tools.
3. **DENY is inherited by descendants.** A third route joins `_DENY_ROUTES`
   (`toolgraph/graph/queries.py:258`):
   `Tool-[READS|WRITES]->R<-[:CONTAINS*1..N]-A-[GOVERNED_BY]->Policy{DENY}`.
   - An inherited row has the meaning of a direct one. It feeds the existing
     `DENY_VIOLATION` and `DENY_GOVERNED` reasons, so `strict`, `review` and
     `explore` treat it exactly as they treat a direct DENY today.
   - The row keeps the touched `R` as `resource` and names `A` in a new field.
     That field joins the sort keys, so two governed ancestors with the same
     policy give a stable order on both backends, and joins `_RESOURCE_KEYS`
     (`toolgraph/artifact_safety.py:162`) so it is redacted like `resource`.
   - The path string shows the chain with one `<-CONTAINS-` segment per hop.
     For `A` contains `B` contains `R`:
     `(tool) -WRITES-> (R) <-CONTAINS- (B) <-CONTAINS- (A) -GOVERNED_BY-> (P:DENY)`.

   Every other reader of GOVERNED_BY decides explicitly:

   | Reader | Treatment |
   |--------|-----------|
   | `check_access`, `unsafe_callable_tools`, selector deny evidence | through `_DENY_ROUTES` |
   | `orphan_policies` | a policy on `A` is not orphaned while a granted tool reaches a descendant |
   | `destructive_unsafeguarded` | a touched resource with a governed ancestor counts as governed |
   | `blast_radius` (resource, policy) | include descendants of the named resource or of each governed resource |
   | selector evidence coverage, `unbacked_edges` (`toolgraph/graph/queries.py:705`) | a CONTAINS edge without evidence counts like any unbacked edge |
   | ingest exception validation | an exception matches on the touched `R`; the wildcard check (`toolgraph/manifest/ingest.py:407`) also counts descendants of a governed resource |
   | `policy_impact._hypothetical` (`toolgraph/policy_impact.py:149`) | projects containment the same way, so a preview agrees with the ingest it previews |
   | `ingest-manifest --dry-run` (`_compute_dry_run_diff`, `toolgraph/manifest/ingest.py:665`) | reports containment additions, removals and provenance changes, and counts containment endpoints in resource retention, so a reparenting that changes an inherited DENY is not shown as "no change" |
   | control-plan exposure records (`toolgraph/control_plan.py:523`) | unchanged: they report edges, not verdicts |

4. **The policy bundle does not change.** An inherited DENY reaches a
   consumer as the tool's existing decision and reason. Only the advisory
   `paths` strings gain the chain form, and the schema lets consumers ignore
   them (`contracts/policy-bundle.schema.json:90`). `schema_version` stays 1
   and memtomem-stm needs no change.
5. **A manifest without containment is unaffected.** Its verdicts stay the
   same. Its governance digest stays the same too, provided the new section
   joins the list `governance_digest` leaves out while empty
   (`toolgraph/manifest/parser.py:18`), as `governed_tools` and
   `governed_resources` did.

## Consequences

- Authoring containment can turn an ALLOW into DENY for every tool with a
  READS/WRITES edge to a descendant. `policy impact` (ADR-0015) previews this
  before ingest, which is why its projection is in the reader table.
- Resources gain one authored edge type, so ADR-0001's prune rule and
  ADR-0007's retirement rules each gain a case.
- A new rejected-bundle golden fixture covers the chain form. The existing
  fixtures, whose digests both repositories pin, keep their bytes.

## Alternatives

- **URI-prefix governance** (#299 as filed): needs a written rule for case,
  trailing `/`, dot segments, percent-encoding, opaque URIs and segment
  boundaries, with handwritten oracles. Deferred to its own ADR, if wanted.
- **Containment inferred from URI paths:** the same normalization problem,
  hidden inside ingest.
- **A DAG instead of a tree:** combining DENYs from several parents would be
  deterministic, but every reader in decision 3 and the dry-run diff would
  have to handle several paths to one ancestor, and no known case needs it.
  A later ADR can lift the restriction.

## Deferred: argument bindings

Binding a tool parameter to the resources its values name (gap 1) is left to
a later ADR, written together with the consumer and after the contest
prototype has been used. The reviews of this ADR's drafts raised these
requirements for it; they are recorded so they are not lost:

- memtomem-stm's default source is the live stdio consult, whose call gate
  checks nothing outside bundle mode; binding enforcement needs a defined
  posture for that source.
- The bundle has no `input_schema`, so an exported set of parameters the
  operator marked as non-targets is needed for a consumer to tell them from
  unclassified ones.
- Gateway-owned arguments (stm's `_context_query`) must be removed before
  evaluation.
- Bindings must not make a tool count as mapped on live surfaces
  (`eligible_tools`, `check_access`), which never see them.
- The exported resource inventory needs a defined membership and the
  identity rule (2) applied at bundle compile time.
- Profiles and per-tool exceptions need a meaning for bound calls, and
  `orphan_policies` must not report policies that only bindings reach.
- Where bindings live on both backends, and how crawl, drift and dry-run
  treat them.

## Open questions

- The value of `N`.
- The manifest field name for containment and for the new row field.

## Acceptance criteria for implementation

- Handwritten oracle tables, never computed by the code under test, for:
  inheritance at depth 1 and `N`; ingest rejection at `N+1`, a second
  parent and a cycle; each identity-rule refusal; each row of the reader
  table.
- The query bound is tested separately on a graph seeded past `N` without
  ingest.
- A #280-style collision case with a containment-only resource is rejected.
- `policy_impact` previews, `--dry-run` and actual ingest agree on
  containment cases, including a reparenting.
- The implementation PR lists every query over Resource, READS, WRITES and
  GOVERNED_BY found by searching the code, not by reading the table in
  decision 3. Three review rounds of this ADR each found a reader the table
  had missed.
- Neo4j and Ladybug return identical rows for every oracle case.
- The prune rule ships in the same PR as CONTAINS.

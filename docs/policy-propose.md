# Proposing grants from observation windows

`toolgraph policy propose governance.yaml observed.jsonl` produces a human-reviewed
proposal. It does not apply grants or change the graph. The JSONL format is
specified by [`observation-window.schema.json`](../contracts/observation-window.schema.json).

The envelope's governance digest must match the supplied manifest. A complete
provenance check also requires an envelope instance ID and a live graph with an
instance ID and governance digest. Both instance IDs, both digests and the graph
generations must agree for an **exact composite token match**. This compares
export declarations; it does not authenticate the observation producer.

- `graph_instance_id` is the envelope's canonical identity field; `instance_id` is
  accepted as a compatibility alias. Conflicting nonempty aliases, non-string IDs,
  control characters and surrounding whitespace on a nonblank ID are errors.
- Missing, null or blank identity, missing live governance digest, or a missing
  envelope is refused by default. `--unverified-provenance` explicitly allows
  these incomplete inputs and labels their provenance **unverified**.
- Explicitly conflicting instance IDs or governance digests are always refused.
  The supplied manifest must match any available live governance digest even
  when the export has no envelope. Invalid supplied fields are not bypassed.
- A changed generation requires `--revalidate-current`. The unverified flag alone
  does not waive a known generation mismatch. Revalidation alone does not waive
  incomplete provenance; both flags are required in that case.

## Checking the current catalog

```sh
toolgraph policy propose governance.yaml observed.jsonl --revalidate-current
toolgraph policy propose governance.yaml observed.jsonl --unverified-provenance --revalidate-current --json
```

`--revalidate-current` checks proposed additions against the **strict** selector,
including when generations already match or the export declares `review` or
`explore`. Each candidate is evaluated as if its grant had been added, without
writing that hypothetical grant to the database. All other current blockers still
apply: live server exposure, authored data flow, tool resolution and DENY paths.

DENY candidates move to `conflicts`; missing, retired or unmapped tools move to
`not_grant_shaped`. Missing live agents and already-granted candidates are recorded
under `insufficient_evidence`. Existing evidence thresholds, ambiguous bare-grant
protection and explicit zero-call removal candidates remain in force. Removal
candidates are not validated as hypothetical additions.

Grant resolution and current selector reads share a graph-state retry bracket.
Concurrent changes retry the entire read; five unstable attempts raise
`StateChurnError`. Candidate queries are grouped by agent in batches of at most
4,096.

## Output provenance

The library's `run_propose()` retains its `(proposal, markdown)` return shape.
The proposal dictionary, also returned by CLI `--json`, adds a `provenance` object:

| Field | Meaning |
| --- | --- |
| `status` | `exact`, `revalidated`, or `unverified` |
| `missing_fields` | List of incomplete inputs; empty for complete provenance |
| `observed_state` | `{instance_id, generation, governance_digest}`, or null without an envelope |
| `current_state` | The same three fields from the bracketed graph read |
| `current_revalidated` | Whether current strict revalidation was requested and performed on addition candidates |
| `revalidation_profile` | `strict` when revalidating, otherwise null |

Complete inputs checked with `--revalidate-current` report `revalidated` even when
the generation matches. Incomplete inputs remain `unverified` after current
revalidation: checking today's catalog cannot establish historical provenance.
Markdown reports the same status and separates observed and current identities.
A successful proposal can contain zero additions; inspect its other categories
for excluded candidates and their reasons.

`--json` and `--output` are mutually exclusive. Failed Markdown file writes
return a redacted error and nonzero exit status without a traceback.

## Preview and diagnostic requirements

For governance change previews, `toolgraph ingest-manifest --governance
GOVERNANCE_YAML --dry-run --json` emits the complete structural delta, including
specific revoked grants, exception changes and resource pruning. A rejected
manifest still returns a JSON report with warnings and exits nonzero. `--json`
requires `--dry-run`; ordinary ingest behavior is unchanged. Uninitialized
embedded databases are rejected with `init-schema` guidance without creating or
modifying database files. `doctor` also opens an existing embedded DB read-only.

Dry-run JSON is a display report, not an identity-preserving replay artifact.
URI credentials, queries and fragments are scrubbed throughout its lists,
warnings and reasons; the underlying in-memory delta is unchanged. Catalog
inspection failures report connectivity/permission guidance, separately from
missing-schema guidance. Corrupt embedded files return a controlled error.

Neo4j preview and diagnostic readers require permission to run `SHOW CONSTRAINTS`
in addition to reading graph data. A data-read role without catalog inspection
permission cannot pass the physical-schema check; grant that permission through
the database administrator. The check does not fall back to version-only
validation when catalog access fails. Ladybug diagnostic/preview reads serialize
across threads for the full session, including retry backoff, to protect handle
lifetime. This helper is not intended for parallel request serving. Neo4j uses
ordinary session routing; callers issue only reads, without driver-enforced
write protection.

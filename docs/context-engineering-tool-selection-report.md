# Toolgraph operations and context-engineering report

## Summary

Toolgraph is an advisory analyzer of the MCP tool ecosystem, not a runtime
blocking gateway. That characteristic makes it a good fit as a context
engineering layer sitting in front of a tool-selection algorithm. Instead of a
selection model choosing tools from natural-language descriptions or embedding
similarity alone, feeding it the permission, resource, policy, provenance and
drift information Toolgraph provides lets you narrow the candidate set more
safely and explain why a tool was chosen.

The recommended path does not start with reinforcement learning. Build
deterministic filtering and score-based ranking first, collect
selection/execution telemetry, then move through offline evaluation and
learning-to-rank before extending to a contextual bandit. Full RL is worth
considering only once there is a long-term plan, multi-step tool chains, a clear
reward signal and a safe experimentation environment.

## Where we are today

Toolgraph already provides these core signals:

- Permission and policy reachability along `Agent -> Tool -> Resource -> Policy`
- Provenance on `CAN_CALL`, `READS`, `WRITES` and `GOVERNED_BY` edges
- `check-access` verdicts: `ALLOW`, `DENY`, `NOT_GRANTED`, `AGENT_NOT_FOUND`,
  `TOOL_NOT_FOUND`, `AMBIGUOUS_TOOL`
- `unsafe-tools` classification into `violation` and `authorized_but_governed`
- Negative-truth audit queries such as `unmapped-tools`, `unbacked-edges` and
  `drift`
- The impact radius of a resource or policy change, via `blast-radius`

None of this answers "is this tool semantically right for the task" directly.
What it does provide, as structured context, is "may this agent choose this tool
right now", "which policies and resources does the call touch" and "how
trustworthy is the basis for that judgement".

## Operational review

### Security

Cypher queries mostly use parameter binding today, and manifest ingest refuses
to apply anything when an unresolved reference is present. Both are good for
operational safety. A production-like deployment still needs the following:

- The default Neo4j credentials and the exposed docker-compose port must be
  treated as local-development-only.
- `toolgraph serve --http` exposes the MCP query surface with no separate
  authentication or authorization layer.
- The stdio `command` entries in `servers.yaml` assume the configuration file is
  operator-trusted.
- HTTP/SSE crawl targets and headers can carry sensitive values, so a redaction
  policy is needed to keep secrets out of logs and provenance.

The minimum bar before an operational deployment: secret rotation, restricted
network binding, reverse-proxy authentication, a separate read-only database
account, and a review process for crawl configuration.

### Performance

The current MVP prioritises explainability and correctness. As fleet size grows,
these costs can become significant:

- `unmapped-tools`, `unbacked-edges`, `drift` and `orphan-policies` are audit
  queries that sweep the whole graph.
- `blast-radius` results grow sharply when a resource or policy has a large
  tool/agent fan-out.
- Crawling has per-server timeout and concurrency limits, but no separate cap on
  the number of tool/resource pages per server.
- The MCP server wrapper returns query results in one shot, so on a large graph
  response size directly affects selection latency.

A sensible optimisation order: pagination, `LIMIT`/cursor, index review driven by
query profiles, a precomputed feature cache for the selector, and cache
invalidation after crawl/ingest.

### Reliability

Manifest ingest clears and reapplies authored edges only for a clean manifest,
which reduces the risk of a partial application producing a false `ALLOW`. The
crawler likewise isolates per-server failures and allows partial success.

What needs strengthening is operational procedure:

- A policy is needed for whether ingest may proceed while crawl failures exist.
- When `drift` results remain, the selector should automatically exclude those
  tools from the candidate set.
- A graph snapshot or export around large changes makes rollback decisions
  easier.
- HTTP serving needs health/readiness endpoints, timeouts, request size limits
  and structured logging.

## Context-engineering model

Splitting tool selection into four stages makes Toolgraph's role clear:

```text
user/task context
  -> candidate generation
  -> graph-aware filtering
  -> graph-aware ranking
  -> execution + telemetry
```

### 1. Candidate generation

Initial candidates can be produced the usual way:

- Tool name / description search
- Embedding similarity
- Task classifier
- Memory of recently successful tools
- Explicit user preference

This stage focuses on recall. No policy judgement happens yet.

### 2. Graph-aware filtering

Toolgraph is used as a hard filter here:

- `AGENT_NOT_FOUND`: stop selection entirely and report it as a context
  construction error.
- `TOOL_NOT_FOUND`: drop from candidates.
- `AMBIGUOUS_TOOL`: do not auto-resolve a bare name; require a server-qualified
  key.
- `NOT_GRANTED`: drop from candidates.
- `DENY` + `violation`: drop from candidates by default.
- `DENY` + `authorized_but_governed`: keep at low priority only when an override
  policy exists.
- `drifted_tools`: drop from candidates, or require operator confirmation.
- `unmapped_tools`: drop for safety-critical tasks; apply a penalty for ordinary
  tasks.

The purpose of this stage is to remove tools the model must not choose *before*
ranking happens.

### 3. Graph-aware ranking

Remaining candidates get a score:

```text
score(tool) =
  task_match_score
  + permission_score
  + provenance_score
  + reliability_score
  - policy_risk_penalty
  - missing_mapping_penalty
  - drift_penalty
  - blast_radius_penalty
  - latency_cost_penalty
```

Recommended features:

| Feature | Source | Effect on selection |
| --- | --- | --- |
| `permitted` | `check-access` | Hard reject when false |
| `verdict` | `check-access` | Prefer `ALLOW`; reject `DENY` depending on policy |
| `classification` | `unsafe-tools` | Reject `violation`; penalise an expected exception |
| `provenance.source` | Query result | Favour `crawled` edges and authored edges with evidence |
| `provenance.confidence` | Query result | Penalise low confidence |
| `has_evidence` | Inverse of `unbacked-edges` | Penalise load-bearing edges with no evidence |
| `is_mapped` | Inverse of `unmapped-tools` | Penalise tools with no authored data flow |
| `is_drifted` | Inverse of `drift` | Reject stale tools |
| `blast_radius_size` | `blast-radius` | Penalise wide-impact tools with care |
| `resource_policy_count` | Graph query | Penalise more contact points with sensitive resources |

### 4. Execution telemetry

To keep improving selection, the selector should emit a log like this:

```json
{
  "event": "tool_selection",
  "agent": "planner",
  "task_hash": "stable-task-or-trace-id",
  "candidate_tools": ["filesystem::read_file", "git::git_show"],
  "selected_tool": "git::git_show",
  "graph_features": {
    "verdict": "ALLOW",
    "is_drifted": false,
    "has_unbacked_edges": false,
    "blast_radius_size": 3
  },
  "ranker_version": "heuristic-2026-06",
  "execution": {
    "success": true,
    "latency_ms": 420,
    "cost_units": 1,
    "retry_count": 0,
    "policy_violation": false
  },
  "feedback": {
    "user_corrected": false,
    "operator_override": false
  }
}
```

Telemetry needs a policy against storing raw prompts or secret-bearing resource
URIs verbatim. Storing task text as a hash or a redacted summary is safer.

## Performance strategy

### Online path

Tool selection sits on the user request path, so the query budget must be small.

- Cap the candidate count first — for example, the top 50 semantic candidates.
- Do not call `check-access` N times; add a batch query instead.
- Keep `drift`, `unmapped` and `unbacked` results in a selector cache.
- Increment a cache generation after crawl/ingest to invalidate stale features.
- Do not compute `blast-radius` for every candidate every time; compute it
  lazily for high-risk candidates only.

### Offline path

Auditing and training-data preparation belong in offline jobs:

- Full `unbacked-edges` and `drift` scans
- Per-policy blast-radius reports
- Feature distribution drift detection
- Failed selection replay
- Ranker weight tuning

### Neo4j query hygiene

Uniqueness constraints currently cover the main node keys. At operational scale,
also review:

- Cardinality of frequently used relationship traversals
- High-degree resource and policy nodes
- Queries with large db hits under `PROFILE`
- Whether audit queries need pagination
- An upper bound on response payloads

## Continuous improvement and the RL question

### Stage 1: heuristic ranker

Early on, a score table a human can read is better. Keep policy violations,
drift and missing evidence as explicit penalties, and use the existing retrieval
score for task matching.

The advantage is debuggability and operational sign-off. The drawback is that
weight tuning is manual.

### Stage 2: offline evaluation

Replay past selection logs to compare what the ranker would have chosen.

Key metrics:

- Task success rate
- Unsafe candidate reject rate
- Unnecessary rejection rate
- Average latency
- Retry count
- User correction rate
- Policy violation count

Without this stage, attaching RL or a bandit makes it hard to tell an
improvement from luck.

### Stage 3: learning-to-rank

Once enough labelled outcomes exist, a feature-based ranker can be trained. Even
then, hard safety constraints stay outside the model:

```text
eligible candidates = graph hard filter(context, candidates)
ranked candidates = learned_ranker(features(eligible candidates))
```

No matter how high a score the model assigns, it must not be able to override a
hard reject such as `NOT_GRANTED`, `violation` or `drifted`.

### Stage 4: contextual bandit

Tool selection is mostly a "which tool do I choose in this context" problem. A
contextual bandit fits before full RL, which deals with long-episode rewards.

A possible reward:

```text
reward =
  + task_success
  - retry_penalty
  - latency_penalty
  - cost_penalty
  - user_correction_penalty
  - policy_violation_penalty
```

Exploration must be constrained in production. For example, exclude candidates
attached to a high-risk policy from exploration and explore only among
equivalent low-risk candidates.

### Stage 5: full RL

Consider full RL only when all of these hold:

- The state and action space of multi-step tool chains is clearly defined
- Delayed reward genuinely matters
- A simulation or replay environment exists
- Safety constraints can be enforced outside the policy
- Training can happen in a sandbox where failure is cheap

Toolgraph's nearest application today is graph-constrained contextual ranking,
not full RL.

## Recommended API extensions

The following APIs make selector integration easier.

> **Erratum (2026-07-09):** the APIs in this section were implemented and
> shipped on 2026-06-11. The normative form is `toolgraph/graph/selector.py`,
> and `tests/test_selector_contract.py` pins it as a consumer contract
> (memtomem-stm). Changes from the draft: `path` (singular) became `paths` (a
> list); the `provenance_score` field was dropped from `rank_features` output
> (the unbacked signal is absorbed into `has_unbacked_edges` plus a risk_score
> of 0.4); `risk_score` comes from a fixed table. The samples below have been
> updated to the implemented shape.

### `rank_features(agent, candidates)`

Returns selection features for several candidate tools in one batch.

```json
{
  "agent": "planner",
  "agent_found": true,
  "features": [
    {
      "candidate": "filesystem::read_file",
      "tool_key": "filesystem::read_file",
      "found": true,
      "permitted": true,
      "verdict": "DENY",
      "classification": "violation",
      "is_drifted": false,
      "is_unmapped": false,
      "has_unbacked_edges": true,
      "risk_score": 1.0
    }
  ]
}
```

### `eligible_tools(agent, candidates, profile)`

Returns the hard-filter result together with reject reasons.

```json
{
  "agent": "planner",
  "agent_found": true,
  "profile": "strict",
  "eligible": ["git::git_status"],
  "rejected": [
    {
      "candidate": "filesystem::read_file",
      "tool_key": "filesystem::read_file",
      "reason": "DENY_VIOLATION",
      "paths": ["(read_file) -READS-> (file:///private/secrets.env) -GOVERNED_BY-> (secrets-deny:DENY)"]
    }
  ]
}
```

### `selection_explain(agent, tool)`

A compact result for explaining the final choice to a user or operator.

```json
{
  "tool_key": "git::git_status",
  "decision": "selected",
  "reasons": [
    "agent has CAN_CALL grant",
    "no DENY policy path found",
    "all load-bearing edges have evidence",
    "tool is currently exposed by crawled server"
  ]
}
```

## Implementation notes against this repository

Given the current code structure, the most natural approach is to add small
pieces in these places:

| Area | Current location | Direction |
| --- | --- | --- |
| Graph query | `toolgraph/graph/queries.py` | Add a batch candidate feature query |
| MCP wrapper | `toolgraph/server/app.py` | Expose `rank_features`, `eligible_tools`, `selection_explain` |
| CLI | `toolgraph/cli.py` | Add commands an operator can verify by hand |
| Models | `toolgraph/models.py` | Add pydantic models for selection features/results |
| Tests | `tests/test_queries.py`, `tests/test_server.py` | Verify the direct query and the MCP shape together |

### Direction for the `rank_features` query

Calling `check-access` repeatedly per candidate makes latency grow linearly with
candidate count. A selector-facing API should take the candidate list at once and
return the following in a batch:

- Tool resolution result: not found, ambiguous, or resolved key
- Whether the agent holds a grant
- Whether a DENY path exists, and its classification
- Whether the tool is drifted
- Whether the tool is unmapped
- Whether load-bearing edges are missing evidence

For the MVP it is enough to compute `risk_score` from rules rather than a learned
model:

```text
risk_score =
  1.0 if DENY violation
  0.8 if drifted
  0.6 if unmapped
  0.4 if unbacked load-bearing edge
  0.2 if authorized_but_governed
  0.0 otherwise
```

### Direction for `eligible_tools` policy

Rather than scattering hard-filter policy through the code, keep it in named
policy profiles:

| Profile | Use | Default behaviour |
| --- | --- | --- |
| `strict` | Production agents | Only `ALLOW` passes |
| `review` | Human-in-the-loop review | Expected exceptions pass; violations are rejected |
| `explore` | Offline eval / sandbox | Only not-granted and drift are rejected; everything else is penalised |

These profiles make selector experiments possible, but the production default
must remain `strict`.

### Direction for telemetry storage

Rather than growing the database schema early, start with append-only JSONL or a
separate event sink. Operationally, reproducibility matters more than training.

Four fields are essential:

- `ranker_version`: which selector logic made the choice
- `graph_generation`: which crawl/ingest state the features came from
- `reject_reasons`: why each candidate was excluded
- `outcome`: execution success, latency, retries, user correction

`graph_generation` does not exist in the code yet, so introducing a run id or
timestamp that increments when crawl/ingest finishes is the starting point for
follow-up work.

## Verification criteria

A selector integration PR should include operational regression tests, not just
functional ones:

- An ambiguous bare tool name must not be auto-selected.
- `DENY` + `violation` candidates must be excluded under the `strict` profile.
- `authorized_but_governed` candidates must be excluded under `strict` and pass
  under `review`.
- Drifted tools must be excluded under every online profile.
- Unmapped tools must be excluded under `strict` or carry an explicit penalty.
- Batch API results must preserve candidate input order and stable ids.
- The MCP wrapper must return the same structured shape as the direct query.
- Telemetry must never store raw secret values or authorization headers.

## Roadmap

1. **Selector adapter**
   Use `check-access`, `drift` and `unmapped-tools` results as a hard filter in
   front of the existing selector.

2. **Batch feature query**
   Add a `rank_features`-style query that evaluates N candidates at once.

3. **Telemetry schema**
   Store selection events, execution outcomes, user corrections and operator
   overrides.

4. **Offline eval**
   Tune heuristic weights against a fixed test set and replay logs.

5. **Feature cache**
   Cache graph-derived features per crawl/ingest generation.

6. **Learning-to-rank**
   Train ranking only over eligible candidates, after the hard filter.

7. **Contextual bandit**
   Introduce constrained exploration among low-risk candidates.

8. **Runtime gateway integration**
   Keep Toolgraph as an analyzer; perform actual blocking in a gateway or the
   execution layer.

## Conclusion

Toolgraph does not replace the "thinking model" of a tool-selection algorithm.
It structures the operational context that selection needs. Putting permissions,
policies, resources, provenance and drift into the selection context lets the
model choose from fewer candidates, at lower risk, with a more explainable
result.

The most valuable next steps are not reinforcement learning, but these three:

1. A graph-aware hard filter
2. A batch rank-feature API
3. Selection telemetry and offline evaluation

Once those three are stable, learning-to-rank and a contextual bandit attach
naturally.

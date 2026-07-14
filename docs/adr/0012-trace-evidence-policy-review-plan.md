# ADR-0012: Trace evidence becomes a human-required policy review plan

## Status

Accepted.

## Context

Tracegraph can identify repeated tool failures, but an observed failure is not
an authorization fact. Automatically converting an accepted finding into a
grant, deny rule, or exception would collapse evidence review and policy
authorship into one unsafe step.

## Decision

`toolgraph policy review-plan` consumes only Tracegraph candidates whose local
Toolgraph disposition is currently `accepted`. It binds them to the exact
current `graph_state`, evaluates the selected agent/profile, and writes a
private, atomic `toolgraph.policy-review-plan` artifact.

Every candidate is stamped with `decision_mode: human_required` at the
artifact level and `automatic_change: false` individually. The command never
edits a governance manifest, graph, selector, or gateway bundle. An operator
must review the plan, author the manifest change, run `ingest-manifest`, inspect
the changed decision/blast radius, and only then compile a new policy bundle.

## Consequences

- Tracegraph evidence is useful in the policy lifecycle without becoming an
  automatic policy authority.
- The accepted review event, current graph state, and eventual gateway bundle
  remain independently auditable artifacts.
- Toolgraph stays a control plane; enforcement remains the gateway consumer's
  responsibility.

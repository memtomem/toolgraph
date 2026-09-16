"""Ingest authored governance into the graph (declarative + idempotent).

The manifest is the single source of truth for authored edges and is applied
atomically: a fully-valid manifest clears all CAN_CALL/READS/WRITES/GOVERNED_BY
edges and re-applies (so revoking a grant in YAML removes it), while a manifest
with ANY unresolved reference is rejected whole and the graph is left untouched.
This matters because a partial apply could clear an existing DENY and skip its
replacement, leaving a false ALLOW. Crawl-owned EXPOSES/PROVIDES are untouched.

Tool references resolve to a unique key ("<server>::<tool>" exact, or a bare
name when it matches exactly one tool). Bare names matching multiple servers,
unknown tools, and undefined policy ids become warnings that abort the ingest.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from neo4j import ManagedTransaction, Session

from toolgraph.graph import queries
from toolgraph.graph.driver import acquire_readonly_session, session
from toolgraph.graph.queries import resolve_tool_refs
from toolgraph.graph.schema import bump_generation, check_backend_schema_version, exception_key
from toolgraph.manifest.parser import governance_digest
from toolgraph.models import Governance, GovernedByBinding, edge_provenance_props
from toolgraph.uris import is_resource_ref, normalize_resource_uri


@dataclass
class IngestReport:
    """Outcome of a manifest ingest (ADR-0002).

    ``warnings`` reject the whole manifest — graph untouched (the atomicity
    contract). ``notices`` are non-fatal advisories: today, drift — a tool
    ref that resolves to a node no server currently exposes. A clean apply
    has empty ``warnings`` but may still carry ``notices``; with
    ``strict_drift`` drift notices are promoted into ``warnings``.
    """

    warnings: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)

    @property
    def applied(self) -> bool:
        return not self.warnings


@dataclass
class GovernanceDiffReport:
    """Outcome of a dry-run manifest ingest."""

    applied: bool = True
    warnings: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    graph_state: dict[str, Any] = field(default_factory=dict)
    agents_created: list[str] = field(default_factory=list)
    agents_deleted: list[str] = field(default_factory=list)
    policies_created: list[dict[str, Any]] = field(default_factory=list)
    policies_deleted: list[str] = field(default_factory=list)
    policy_properties_modified: list[dict[str, Any]] = field(default_factory=list)
    grants_added: list[dict[str, Any]] = field(default_factory=list)
    grants_revoked: list[dict[str, Any]] = field(default_factory=list)
    granted_by_modified: list[dict[str, Any]] = field(default_factory=list)
    data_access_added: list[dict[str, Any]] = field(default_factory=list)
    data_access_removed: list[dict[str, Any]] = field(default_factory=list)
    governed_by_added: list[dict[str, Any]] = field(default_factory=list)
    governed_by_removed: list[dict[str, Any]] = field(default_factory=list)
    provenance_modified: list[dict[str, Any]] = field(default_factory=list)
    expected_exceptions_added: list[dict[str, Any]] = field(default_factory=list)
    expected_exceptions_removed: list[dict[str, Any]] = field(default_factory=list)
    expected_exceptions_reason_updated: list[dict[str, Any]] = field(default_factory=list)
    resources_created: list[str] = field(default_factory=list)
    resources_pruned: list[str] = field(default_factory=list)
    resources_retained_crawled: list[str] = field(default_factory=list)
    tools_pruned: list[str] = field(default_factory=list)
    tools_retained_crawled: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary_text(self) -> str:
        lines = []
        if not self.applied:
            lines.append("DRY-RUN RESULT: REJECTED (warnings present)")
            for w in self.warnings:
                lines.append(f"  [warn] {w}")
            return "\n".join(lines)
        lines.append("DRY-RUN RESULT: VALID (would apply cleanly)")
        lines.append(f"  Graph state token: {self.graph_state}")
        lines.append("  Delta summary:")
        lines.append(f"    Agents: +{len(self.agents_created)} / -{len(self.agents_deleted)}")
        lines.append(
            f"    Policies: +{len(self.policies_created)} / -{len(self.policies_deleted)} / ~{len(self.policy_properties_modified)}"
        )
        lines.append(
            f"    Grants: +{len(self.grants_added)} / -{len(self.grants_revoked)} / ~{len(self.granted_by_modified)} (granted_by)"
        )
        lines.append(f"    Data access: +{len(self.data_access_added)} / -{len(self.data_access_removed)}")
        lines.append(f"    Governed by: +{len(self.governed_by_added)} / -{len(self.governed_by_removed)}")
        lines.append(
            f"    Exceptions: +{len(self.expected_exceptions_added)} / -{len(self.expected_exceptions_removed)} / ~{len(self.expected_exceptions_reason_updated)}"
        )
        lines.append(f"    Provenance modified: {len(self.provenance_modified)}")
        lines.append(
            f"    Resources: +{len(self.resources_created)} created / -{len(self.resources_pruned)} pruned (final degree 0) / {len(self.resources_retained_crawled)} retained crawled"
        )
        lines.append(
            f"    Tools: -{len(self.tools_pruned)} pruned (final degree 0) / {len(self.tools_retained_crawled)} retained crawled"
        )
        return "\n".join(lines)


def _normalize_bindings(items: list) -> list[GovernedByBinding]:
    """Lift heterogeneous list items to GovernedByBinding instances.

    Three input shapes can land here:
    - ``str`` — bare policy id (backward-compat YAML shape; or list[str]
      injected via Pydantic ``model_copy(update=...)`` which BYPASSES
      validators; test_review_fixes.py:223 hits exactly this path).
    - ``dict`` — raw object from ``model_copy(update=...)`` that wasn't
      re-validated; coerce via ``model_validate``.
    - ``GovernedByBinding`` — already-validated object from the YAML parser.
    """
    out: list[GovernedByBinding] = []
    for item in items:
        if isinstance(item, str):
            out.append(GovernedByBinding(policy=item))
        elif isinstance(item, GovernedByBinding):
            out.append(item)
        else:
            out.append(GovernedByBinding.model_validate(item))
    return out


def _resolve(resolved: dict[str, list[str]], ref: str) -> tuple[str | None, list[str]]:
    """Return (unique_key, all_matches). unique_key is set only on exactly one match.

    Shares the exact resolution rule with check_access so write-side and
    read-side tool identity can never drift.
    """
    keys = resolved[ref]
    return (keys[0] if len(keys) == 1 else None, keys)


def _resolve_problem(ref: str, matches: list[str]) -> str:
    return f"ambiguous {matches}" if matches else "not found"


def _validate_manifest(
    tx: ManagedTransaction | Session,
    gov: Governance,
    strict_drift: bool = False,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Phase 1 read-only validation; returns (warnings, notices, resolved)."""
    warnings: list[str] = []
    notices: list[str] = []
    # Detect duplicate policy ids BEFORE building deny_policies. Without this,
    # a manifest with [Policy(id='x', effect='DENY'), Policy(id='x', effect='ALLOW')]
    # passes deny-policy validation (set-based), then Phase 2 MERGE+SET on the
    # same id silently overwrites effect to whichever was processed last —
    # potentially leaving an ALLOW node that no longer matches the DENY-only
    # classifier in _deny_evidence_for. Codex R3.5 review caught this.
    seen_policy_ids: set[str] = set()
    for p in gov.policies:
        if p.id in seen_policy_ids:
            warnings.append(
                f"POLICY: duplicate id {p.id!r} in policies: list — "
                f"each policy must be declared once"
            )
        seen_policy_ids.add(p.id)
    known_policies = {p.id for p in gov.policies}
    deny_policies = {p.id for p in gov.policies if p.effect == "DENY"}
    known_agents = set(gov.agents) | {g.agent for g in gov.grants}

    # --- Phase 1: validate + resolve (read-only) -------------------------
    # Resolve once on this transaction; validation still walks the original
    # manifest order so diagnostics and last-entry-wins semantics do not move.
    refs = [g.tool for g in gov.grants] + [d.tool for d in gov.data_access]
    refs.extend(ref for ref in gov.governed_by if not is_resource_ref(ref))
    refs.extend(e.tool for e in gov.expected_exceptions)
    resolved_refs = resolve_tool_refs(tx, refs)
    resolved_grants: list[tuple] = []
    for grant in gov.grants:
        key, matches = _resolve(resolved_refs, grant.tool)
        if key is None:
            warnings.append(
                f"CAN_CALL: tool {grant.tool!r} {_resolve_problem(grant.tool, matches)} "
                f"(agent {grant.agent!r})"
            )
        else:
            resolved_grants.append(
                (grant.agent, key, grant.granted_by, edge_provenance_props(grant.provenance))
            )

    resolved_access: list[tuple] = []
    authored_access: set[tuple[str, str]] = set()  # (tool_key, resource_uri) for exception check
    for da in gov.data_access:
        key, matches = _resolve(resolved_refs, da.tool)
        if key is None:
            warnings.append(f"{da.mode}: tool {da.tool!r} {_resolve_problem(da.tool, matches)}")
        else:
            uri = normalize_resource_uri(da.resource)
            resolved_access.append((key, da.mode, uri, edge_provenance_props(da.provenance)))
            authored_access.add((key, uri))

    # Resolve governed_by first so exception validation can see the full
    # (resource→policy) and (tool→policy) reachability map.
    resolved_governed: list[tuple] = []  # (kind, key_or_uri, list[(pid, prov_props)])
    policy_resources: dict[str, set[str]] = {}  # pid -> {uri, ...}
    policy_tools: dict[str, set[str]] = {}      # pid -> {tool_key, ...}
    for node_ref, items in gov.governed_by.items():
        bindings = _normalize_bindings(items)
        for b in bindings:
            if b.policy not in known_policies:
                warnings.append(
                    f"GOVERNED_BY: policy {b.policy!r} not defined (node {node_ref!r})"
                )
        valid = [
            (b.policy, edge_provenance_props(b.provenance))
            for b in bindings
            if b.policy in known_policies
        ]
        if is_resource_ref(node_ref):
            uri = normalize_resource_uri(node_ref)
            resolved_governed.append(("resource", uri, valid))
            for pid, _ in valid:
                policy_resources.setdefault(pid, set()).add(uri)
        else:
            key, matches = _resolve(resolved_refs, node_ref)
            if key is None:
                warnings.append(
                    f"GOVERNED_BY: tool {node_ref!r} {_resolve_problem(node_ref, matches)}"
                )
            else:
                resolved_governed.append(("tool", key, valid))
                for pid, _ in valid:
                    policy_tools.setdefault(pid, set()).add(key)

    # Reachability lookups for exception validation: which tools each agent
    # holds a grant on, and which resources each tool actually touches.
    agent_grant_tools: dict[str, set[str]] = {}
    for a, k, _, _ in resolved_grants:
        agent_grant_tools.setdefault(a, set()).add(k)
    tool_resources: dict[str, set[str]] = {}
    for k, _, uri, _ in resolved_access:
        tool_resources.setdefault(k, set()).add(uri)

    resolved_exceptions: list[tuple] = []  # (agent, tool_key, resource_or_None, policy, reason)
    for exc in gov.expected_exceptions:
        # Validate the agent against declared agents (or grant authors) so a
        # typo like 'planer' doesn't silently create a dead-on-arrival
        # exception node that fails to classify any row.
        if exc.agent not in known_agents:
            warnings.append(
                f"EXPECTED_EXCEPTION: agent {exc.agent!r} not declared in agents: or grants: "
                f"(tool {exc.tool!r})"
            )
            continue
        if exc.policy not in known_policies:
            warnings.append(
                f"EXPECTED_EXCEPTION: policy {exc.policy!r} not defined "
                f"(agent {exc.agent!r}, tool {exc.tool!r})"
            )
            continue
        # Classification only joins to DENY policies (_deny_evidence_for matches
        # `Policy {effect:'DENY'}`); an exception against an ALLOW policy can
        # never fire. Codex R3 review caught this.
        if exc.policy not in deny_policies:
            warnings.append(
                f"EXPECTED_EXCEPTION: policy {exc.policy!r} has effect=ALLOW; "
                f"exceptions only apply to DENY policies "
                f"(agent {exc.agent!r}, tool {exc.tool!r})"
            )
            continue
        key, matches = _resolve(resolved_refs, exc.tool)
        if key is None:
            warnings.append(
                f"EXPECTED_EXCEPTION: tool {exc.tool!r} {_resolve_problem(exc.tool, matches)} "
                f"(agent {exc.agent!r})"
            )
            continue
        # The exception only fires when the agent's deny_evidence row matches
        # (agent, tool, resource?, policy). That requires:
        #   1) agent holds a CAN_CALL grant on the tool — otherwise the agent
        #      will never appear in unsafe_callable_tools for that tool;
        #   2) the (tool, policy) pair is actually reachable — either via a
        #      governed resource the tool touches, or by direct tool-level
        #      GOVERNED_BY. Without (2) no deny row will ever fire.
        # Closing only the "wrong resource" slice (the previous round) wasn't
        # enough — Codex flagged that wrong-policy and ungranted-agent
        # exceptions still survived.
        if key not in agent_grant_tools.get(exc.agent, set()):
            warnings.append(
                f"EXPECTED_EXCEPTION: agent {exc.agent!r} has no CAN_CALL grant on tool "
                f"{exc.tool!r} — exception will never classify any row"
            )
            continue
        resource_uri = normalize_resource_uri(exc.resource) if exc.resource else None
        policy_resource_set = policy_resources.get(exc.policy, set())
        policy_tool_set = policy_tools.get(exc.policy, set())
        if resource_uri is not None:
            # Specific resource: tool must READ/WRITE it AND that resource
            # must be GOVERNED_BY this policy.
            if (key, resource_uri) not in authored_access:
                warnings.append(
                    f"EXPECTED_EXCEPTION: tool {exc.tool!r} has no READS/WRITES "
                    f"to resource {exc.resource!r} in this manifest "
                    f"(agent {exc.agent!r}, policy {exc.policy!r})"
                )
                continue
            if resource_uri not in policy_resource_set:
                warnings.append(
                    f"EXPECTED_EXCEPTION: resource {exc.resource!r} is not governed_by "
                    f"policy {exc.policy!r} in this manifest "
                    f"(agent {exc.agent!r}, tool {exc.tool!r})"
                )
                continue
        else:
            # Wildcard: tool must reach this policy via SOME path —
            # a tool-touched resource governed by the policy, OR the tool
            # itself directly governed by the policy.
            reaches_via_resource = bool(
                tool_resources.get(key, set()) & policy_resource_set
            )
            reaches_via_tool = key in policy_tool_set
            if not (reaches_via_resource or reaches_via_tool):
                warnings.append(
                    f"EXPECTED_EXCEPTION: tool {exc.tool!r} has no path to DENY policy "
                    f"{exc.policy!r} in this manifest "
                    f"(agent {exc.agent!r}) — wildcard exception is dead-on-arrival"
                )
                continue
        resolved_exceptions.append(
            (exc.agent, key, resource_uri, exc.policy, exc.reason)
        )

    # ADR-0002 drift contract: a ref can resolve to a Tool node that no
    # server currently exposes (the loader keeps governed tools so a DENY
    # never silently becomes ALLOW). Without this check, re-ingesting the
    # same manifest silently re-grants against the stale node and the drift
    # persists with zero signal in the authoring loop.
    referenced_keys = {k for _, k, _, _ in resolved_grants}
    referenced_keys |= {k for k, _, _, _ in resolved_access}
    referenced_keys |= {ref for kind, ref, _ in resolved_governed if kind == "tool"}
    referenced_keys |= {k for _, k, _, _, _ in resolved_exceptions}
    if referenced_keys:
        drifted = [
            r["key"]
            for r in tx.run(
                "MATCH (t:Tool) WHERE t.key IN $keys "
                "AND NOT EXISTS { MATCH (:MCPServer)-[:EXPOSES]->(t) } "
                "RETURN t.key AS key ORDER BY key",
                keys=sorted(referenced_keys),
            )
        ]
        for key in drifted:
            (warnings if strict_drift else notices).append(
                f"DRIFT: tool {key!r} resolves but no server currently exposes "
                f"it — authored governance targets a stale tool. Remove it from "
                f"the manifest, or re-crawl its server if it should still exist."
            )

    resolved = {
        "resolved_grants": resolved_grants,
        "resolved_access": resolved_access,
        "resolved_governed": resolved_governed,
        "resolved_exceptions": resolved_exceptions,
        "known_agents": known_agents,
        "known_policies": known_policies,
        "deny_policies": deny_policies,
    }
    return warnings, notices, resolved


def _ingest(
    tx: ManagedTransaction,
    gov: Governance,
    strict_drift: bool = False,
    digest: str | None = None,
) -> tuple[list[str], list[str]]:
    """Two phases in one transaction; returns (warnings, notices).

    Phase 1 validates every reference read-only and collects warnings. If ANY
    warning is found the manifest is rejected whole — nothing is mutated — so a
    typo can never wipe an existing DENY and leave a false ALLOW behind. Phase 2
    runs only for a fully clean manifest: clear authored edges, then re-apply.

    Drift (a resolved tool ref with no live EXPOSES edge) is a NOTICE, not a
    warning: rejecting would make a fleet with one temporarily-down server
    un-ingestable. ``strict_drift`` promotes drift to warnings for CI use.
    """
    warnings, notices, resolved = _validate_manifest(tx, gov, strict_drift)
    if warnings:  # reject the whole manifest — do not mutate
        return warnings, notices

    resolved_grants = resolved["resolved_grants"]
    resolved_access = resolved["resolved_access"]
    resolved_governed = resolved["resolved_governed"]
    resolved_exceptions = resolved["resolved_exceptions"]
    known_agents = resolved["known_agents"]

    # --- Phase 2: mutate (clean manifest only) ---------------------------
    # Deliberate size tradeoff: this transaction scales with the total number
    # of authored edges and cannot be batched — a partial apply could clear a
    # DENY and leave a false ALLOW (see the module docstring / ADR-0002).
    tx.run("MATCH ()-[r:CAN_CALL|READS|WRITES|GOVERNED_BY]->() DELETE r")
    # ExpectedException nodes are pure authored bookkeeping — clear and re-apply
    # alongside the edges so a removed exception promotes the row back to violation.
    tx.run("MATCH (e:ExpectedException) DETACH DELETE e")

    tx.run("UNWIND $ids AS id MERGE (:Agent {id:id})", ids=sorted(known_agents))
    # Policy provenance lives on the GOVERNED_BY binding (per-edge), not the
    # node — see GovernedByBinding. Policy nodes carry only declaration data.
    # Null assignments drop the legacy ``prov_*`` properties left over from the
    # pre-completion-round shape, so an upgrading graph cleans itself up.
    if gov.policies:
        tx.run(
            """
            UNWIND $policies AS p
            MERGE (pol:Policy {id:p.id})
            SET pol.effect=p.effect, pol.scope=p.scope, pol.description=p.description
            SET pol.prov_source=null, pol.prov_confidence=null, pol.prov_evidence=null
            """,
            policies=[p.model_dump() for p in gov.policies],
        )
    # Declarative manifest: prune Agent + Policy nodes the operator removed
    # from this manifest. The new ``found`` / ``agent_found`` API treats
    # node existence as a truth signal; leaking renamed-away nodes would
    # silently make ``unsafe-tools old-agent`` look like a clean empty
    # answer and ``blast-radius old-policy`` claim found:true. Edges were
    # already cleared at the top of Phase 2, so DETACH DELETE is a no-op
    # on the edge side; it just removes the now-orphan node.
    policy_ids = [p.id for p in gov.policies]
    tx.run(
        "MATCH (a:Agent) WHERE NOT a.id IN $ids DETACH DELETE a",
        ids=sorted(known_agents),
    )
    tx.run(
        "MATCH (pol:Policy) WHERE NOT pol.id IN $ids DETACH DELETE pol",
        ids=policy_ids,
    )

    # Deduplicate to last-entry-wins BEFORE batching: the old per-row loops
    # deterministically left the later manifest entry's metadata on a
    # duplicate logical edge, while UNWIND gives repeated SETs on one
    # relationship an undefined winner. dict insertion order preserves the
    # manifest order, and overwriting keeps the last occurrence.
    resolved_grants = list({(row[0], row[1]): row for row in resolved_grants}.values())
    resolved_access = list(
        {(row[0], row[1], row[2]): row for row in resolved_access}.values()
    )
    if resolved_grants:
        # One UNWIND batch instead of a round trip per grant (the same shape
        # policies and exceptions already use).
        tx.run(
            """
            UNWIND $rows AS row
            MERGE (a:Agent {id:row.agent})
            WITH a, row MATCH (t:Tool {key:row.key})
            MERGE (a)-[r:CAN_CALL]->(t)
            SET r.granted_by=row.granted_by,
                r.source=row.source, r.confidence=row.confidence, r.evidence=row.evidence
            """,
            rows=[
                {"agent": agent, "key": key, "granted_by": granted_by, **prov}
                for agent, key, granted_by, prov in resolved_grants
            ],
        )

    # mode is a validated Literal -> safe to inline as the relationship type;
    # one UNWIND batch per relationship type instead of a round trip per edge.
    for mode in ("READS", "WRITES"):
        batch = [
            {"key": key, "uri": uri, **prov}
            for key, row_mode, uri, prov in resolved_access
            if row_mode == mode
        ]
        if not batch:
            continue
        tx.run(
            f"""
            UNWIND $rows AS row
            MATCH (t:Tool {{key:row.key}})
            MERGE (res:Resource {{uri:row.uri}})
            MERGE (t)-[r:{mode}]->(res)
            SET r.source=row.source, r.confidence=row.confidence, r.evidence=row.evidence
            """,
            rows=batch,
        )

    resolved_exceptions = list({row[:4]: row for row in resolved_exceptions}.values())
    if resolved_exceptions:
        tx.run(
            """
            UNWIND $rows AS row
            MERGE (e:ExpectedException {key:row.key})
            SET e.agent=row.agent, e.tool_key=row.tool_key,
                e.resource=row.resource, e.policy=row.policy, e.reason=row.reason
            """,
            rows=[
                {
                    "key": exception_key(a, k, r, p),
                    "agent": a,
                    "tool_key": k,
                    "resource": r,
                    "policy": p,
                    "reason": reason,
                }
                for a, k, r, p, reason in resolved_exceptions
            ],
        )

    # GOVERNED_BY edges carry per-binding provenance: an author can cite WHY
    # this resource (or tool) is bound to this policy. Bare-string entries in
    # ``governed_by`` lift to bindings with no evidence (operator_asserted/high
    # by default), so unbacked_edges still surfaces them as needing a citation.
    resource_bindings = [
        {"ref": ref, "pid": pid, **prov}
        for kind, ref, bindings in resolved_governed
        if kind == "resource"
        for pid, prov in bindings
    ]
    tool_bindings = [
        {"ref": ref, "pid": pid, **prov}
        for kind, ref, bindings in resolved_governed
        if kind != "resource"
        for pid, prov in bindings
    ]
    # Same last-entry-wins rule for duplicate (node, policy) bindings.
    resource_bindings = list(
        {(row["ref"], row["pid"]): row for row in resource_bindings}.values()
    )
    tool_bindings = list(
        {(row["ref"], row["pid"]): row for row in tool_bindings}.values()
    )
    if resource_bindings:
        tx.run(
            """
            UNWIND $rows AS row
            MERGE (res:Resource {uri:row.ref})
            WITH res, row MATCH (p:Policy {id:row.pid})
            MERGE (res)-[r:GOVERNED_BY]->(p)
            SET r.source=row.source, r.confidence=row.confidence, r.evidence=row.evidence
            """,
            rows=resource_bindings,
        )
    if tool_bindings:
        tx.run(
            """
            UNWIND $rows AS row
            MATCH (t:Tool {key:row.ref})
            MATCH (p:Policy {id:row.pid})
            MERGE (t)-[r:GOVERNED_BY]->(p)
            SET r.source=row.source, r.confidence=row.confidence, r.evidence=row.evidence
            """,
            rows=tool_bindings,
        )

    # ADR-0001: node existence is a truth signal, for Resources too. A
    # resource the manifest stopped mentioning had its authored edges cleared
    # above and nothing re-applied; leaving the node would make blast_radius
    # answer found:true / impacted:[] for it — indistinguishable from a real
    # but untouched resource. Crawled resources keep their PROVIDES edge and
    # never match this degree-0 filter; the loader stays their owner.
    tx.run("MATCH (res:Resource) WHERE NOT EXISTS { MATCH (res)--() } DELETE res")

    # Same rule for Tools (ADR-0007). A tool kept only for its authored
    # governance — typically after fleet reconciliation retired its server —
    # loses its last edge when the manifest stops mentioning it. No crawl
    # will ever reconcile a retired server's name again, so without this a
    # degree-0 tool ghosts in bare-name resolution forever. Live crawled
    # tools always carry EXPOSES and never match.
    tx.run("MATCH (t:Tool) WHERE NOT EXISTS { MATCH (t)--() } DELETE t")

    # The authored manifest is the source of truth. Persist its semantic
    # digest in the same transaction as the derived graph and generation so a
    # policy-bundle compiler can reject stale graph/file combinations.
    tx.run(
        "MERGE (m:GraphMeta {id:'singleton'}) "
        "SET m.governance_digest=$digest",
        digest=digest,
    )

    # ADR-0004: only a manifest that actually applied bumps the generation —
    # the rejected path returned before Phase 2, leaving caches valid.
    bump_generation(tx)

    return [], notices


def ingest_governance(gov: Governance, strict_drift: bool = False) -> IngestReport:
    """Apply the manifest atomically.

    Non-empty ``report.warnings`` means rejected — graph untouched.
    ``report.notices`` are non-fatal drift advisories (see IngestReport).
    """
    with session() as s:
        warnings, notices = s.execute_write(
            _ingest, gov, strict_drift, governance_digest(gov)
        )
        return IngestReport(warnings=warnings, notices=notices)


def _compute_dry_run_diff(
    s: Session,
    gov: Governance,
    strict_drift: bool = False,
) -> dict[str, Any]:
    warnings, notices, resolved = _validate_manifest(s, gov, strict_drift)
    if warnings:
        return {
            "applied": False,
            "warnings": warnings,
            "notices": notices,
        }

    # Agents
    db_agents = {r["id"] for r in s.run("MATCH (a:Agent) RETURN a.id AS id")}
    prop_agents = set(resolved["known_agents"])
    agents_created = sorted(prop_agents - db_agents)
    agents_deleted = sorted(db_agents - prop_agents)

    # Policies
    db_policies = {
        r["id"]: {
            "effect": r["effect"],
            "scope": r["scope"],
            "description": r["description"],
        }
        for r in s.run(
            "MATCH (p:Policy) RETURN p.id AS id, p.effect AS effect, p.scope AS scope, p.description AS description"
        )
    }
    prop_policies = {
        p.id: {
            "effect": p.effect,
            "scope": p.scope,
            "description": p.description,
        }
        for p in gov.policies
    }
    policies_created = [
        {"id": pid, **prop_policies[pid]}
        for pid in sorted(set(prop_policies) - set(db_policies))
    ]
    policies_deleted = sorted(set(db_policies) - set(prop_policies))
    policy_properties_modified = []
    for pid in sorted(set(db_policies) & set(prop_policies)):
        if db_policies[pid] != prop_policies[pid]:
            policy_properties_modified.append({
                "id": pid,
                "before": db_policies[pid],
                "after": prop_policies[pid],
            })

    # Grants
    db_grants = {}
    for row in s.run(
        "MATCH (a:Agent)-[r:CAN_CALL]->(t:Tool) "
        "RETURN a.id AS agent, t.key AS tool, r.granted_by AS granted_by, "
        "r.source AS source, r.confidence AS confidence, r.evidence AS evidence"
    ):
        db_grants[(row["agent"], row["tool"])] = {
            "granted_by": row["granted_by"],
            "provenance": {
                "source": row["source"],
                "confidence": row["confidence"],
                "evidence": row["evidence"],
            },
        }
    prop_grants = {}
    for agent, key, granted_by, prov_props in resolved["resolved_grants"]:
        prop_grants[(agent, key)] = {
            "granted_by": granted_by,
            "provenance": prov_props,
        }
    grants_added = [
        {"agent": a, "tool": k, "granted_by": info["granted_by"], "provenance": info["provenance"]}
        for (a, k), info in sorted(prop_grants.items())
        if (a, k) not in db_grants
    ]
    grants_revoked = [
        {"agent": a, "tool": k}
        for (a, k) in sorted(db_grants.keys())
        if (a, k) not in prop_grants
    ]
    granted_by_modified = []
    provenance_modified = []
    for a, k in sorted(set(db_grants) & set(prop_grants)):
        if db_grants[(a, k)]["granted_by"] != prop_grants[(a, k)]["granted_by"]:
            granted_by_modified.append({
                "agent": a,
                "tool": k,
                "before": db_grants[(a, k)]["granted_by"],
                "after": prop_grants[(a, k)]["granted_by"],
            })
        if db_grants[(a, k)]["provenance"] != prop_grants[(a, k)]["provenance"]:
            provenance_modified.append({
                "kind": "grant",
                "agent": a,
                "tool": k,
                "before": db_grants[(a, k)]["provenance"],
                "after": prop_grants[(a, k)]["provenance"],
            })

    # Data Access
    db_access = {}
    for row in s.run(
        "MATCH (t:Tool)-[r:READS]->(res:Resource) "
        "RETURN t.key AS tool, 'READS' AS mode, res.uri AS resource, "
        "r.source AS source, r.confidence AS confidence, r.evidence AS evidence "
        "UNION ALL "
        "MATCH (t:Tool)-[r:WRITES]->(res:Resource) "
        "RETURN t.key AS tool, 'WRITES' AS mode, res.uri AS resource, "
        "r.source AS source, r.confidence AS confidence, r.evidence AS evidence"
    ):
        db_access[(row["tool"], row["mode"], row["resource"])] = {
            "source": row["source"],
            "confidence": row["confidence"],
            "evidence": row["evidence"],
        }
    prop_access = {}
    for key, mode, uri, prov_props in resolved["resolved_access"]:
        prop_access[(key, mode, uri)] = prov_props
    data_access_added = [
        {"tool": k, "mode": m, "resource": u, "provenance": prov}
        for (k, m, u), prov in sorted(prop_access.items())
        if (k, m, u) not in db_access
    ]
    data_access_removed = [
        {"tool": k, "mode": m, "resource": u}
        for (k, m, u) in sorted(db_access.keys())
        if (k, m, u) not in prop_access
    ]
    for k, m, u in sorted(set(db_access) & set(prop_access)):
        if db_access[(k, m, u)] != prop_access[(k, m, u)]:
            provenance_modified.append({
                "kind": "data_access",
                "tool": k,
                "mode": m,
                "resource": u,
                "before": db_access[(k, m, u)],
                "after": prop_access[(k, m, u)],
            })

    # Governed By
    db_governed = {}
    for row in s.run(
        "MATCH (res:Resource)-[r:GOVERNED_BY]->(p:Policy) "
        "RETURN 'resource' AS kind, res.uri AS ref, p.id AS policy, "
        "r.source AS source, r.confidence AS confidence, r.evidence AS evidence "
        "UNION ALL "
        "MATCH (t:Tool)-[r:GOVERNED_BY]->(p:Policy) "
        "RETURN 'tool' AS kind, t.key AS ref, p.id AS policy, "
        "r.source AS source, r.confidence AS confidence, r.evidence AS evidence"
    ):
        db_governed[(row["kind"], row["ref"], row["policy"])] = {
            "source": row["source"],
            "confidence": row["confidence"],
            "evidence": row["evidence"],
        }
    prop_governed = {}
    for kind, key_or_uri, valid_bindings in resolved["resolved_governed"]:
        for pid, prov_props in valid_bindings:
            prop_governed[(kind, key_or_uri, pid)] = prov_props
    governed_by_added = [
        {"kind": kind, "ref": ref, "policy": pid, "provenance": prov}
        for (kind, ref, pid), prov in sorted(prop_governed.items())
        if (kind, ref, pid) not in db_governed
    ]
    governed_by_removed = [
        {"kind": kind, "ref": ref, "policy": pid}
        for (kind, ref, pid) in sorted(db_governed.keys())
        if (kind, ref, pid) not in prop_governed
    ]
    for kind, ref, pid in sorted(set(db_governed) & set(prop_governed)):
        if db_governed[(kind, ref, pid)] != prop_governed[(kind, ref, pid)]:
            provenance_modified.append({
                "kind": "governed_by",
                "node_kind": kind,
                "ref": ref,
                "policy": pid,
                "before": db_governed[(kind, ref, pid)],
                "after": prop_governed[(kind, ref, pid)],
            })

    # Expected Exceptions
    db_exceptions = {}
    for row in s.run(
        "MATCH (e:ExpectedException) "
        "RETURN e.agent AS agent, e.tool_key AS tool, e.resource AS resource, "
        "e.policy AS policy, e.reason AS reason"
    ):
        db_exceptions[(row["agent"], row["tool"], row["resource"], row["policy"])] = row["reason"]
    prop_exceptions = {}
    for agent, tool_key, resource_uri, policy_id, reason in resolved["resolved_exceptions"]:
        prop_exceptions[(agent, tool_key, resource_uri, policy_id)] = reason

    def _exc_key(key: tuple[str, str, str | None, str]) -> tuple[str, str, bool, str, str]:
        agent, tool, resource, policy = key
        return agent, tool, resource is not None, resource or "", policy

    expected_exceptions_added = [
        {"agent": a, "tool": t, "resource": r, "policy": p, "reason": reason}
        for (a, t, r, p), reason in sorted(prop_exceptions.items(), key=lambda item: _exc_key(item[0]))
        if (a, t, r, p) not in db_exceptions
    ]
    expected_exceptions_removed = [
        {"agent": a, "tool": t, "resource": r, "policy": p}
        for (a, t, r, p) in sorted(db_exceptions.keys(), key=_exc_key)
        if (a, t, r, p) not in prop_exceptions
    ]
    expected_exceptions_reason_updated = [
        {
            "agent": a,
            "tool": t,
            "resource": r,
            "policy": p,
            "before": db_exceptions[(a, t, r, p)],
            "after": prop_exceptions[(a, t, r, p)],
        }
        for (a, t, r, p) in sorted(set(db_exceptions) & set(prop_exceptions), key=_exc_key)
        if db_exceptions[(a, t, r, p)] != prop_exceptions[(a, t, r, p)]
    ]

    # Whole-graph final degree calculations (ADR-0001, ADR-0007)
    db_resources = {r["uri"] for r in s.run("MATCH (res:Resource) RETURN res.uri AS uri")}
    crawled_resources = {
        r["uri"]
        for r in s.run("MATCH (:MCPServer)-[:PROVIDES]->(res:Resource) RETURN res.uri AS uri")
    }
    prop_authored_resources = {
        uri for _, _, uri, _ in resolved["resolved_access"]
    } | {
        ref for kind, ref, bindings in resolved["resolved_governed"] if kind == "resource" and bindings
    }
    resources_created = sorted(prop_authored_resources - db_resources)
    resources_pruned = sorted([
        uri
        for uri in db_resources
        if uri not in crawled_resources and uri not in prop_authored_resources
    ])
    lost_authored_resources = {uri for (_, _, uri) in db_access} | {
        ref for (kind, ref, _) in db_governed if kind == "resource"
    }
    resources_retained_crawled = sorted([
        uri
        for uri in (lost_authored_resources - prop_authored_resources)
        if uri in crawled_resources
    ])

    db_tools = {r["key"] for r in s.run("MATCH (t:Tool) RETURN t.key AS key")}
    crawled_tools = {
        r["key"] for r in s.run("MATCH (:MCPServer)-[:EXPOSES]->(t:Tool) RETURN t.key AS key")
    }
    prop_authored_tools = (
        {k for (_, k) in prop_grants}
        | {k for (k, _, _) in prop_access}
        | {ref for (kind, ref, bindings) in resolved["resolved_governed"] if kind == "tool" and bindings}
    )
    tools_pruned = sorted([
        t for t in db_tools if t not in crawled_tools and t not in prop_authored_tools
    ])
    lost_authored_tools = (
        {t for (_, t) in db_grants}
        | {t for (t, _, _) in db_access}
        | {ref for (kind, ref, _) in db_governed if kind == "tool"}
    )
    tools_retained_crawled = sorted([
        t for t in (lost_authored_tools - prop_authored_tools) if t in crawled_tools
    ])

    return {
        "applied": True,
        "warnings": [],
        "notices": notices,
        "agents_created": agents_created,
        "agents_deleted": agents_deleted,
        "policies_created": policies_created,
        "policies_deleted": policies_deleted,
        "policy_properties_modified": policy_properties_modified,
        "grants_added": grants_added,
        "grants_revoked": grants_revoked,
        "granted_by_modified": granted_by_modified,
        "data_access_added": data_access_added,
        "data_access_removed": data_access_removed,
        "governed_by_added": governed_by_added,
        "governed_by_removed": governed_by_removed,
        "provenance_modified": provenance_modified,
        "expected_exceptions_added": expected_exceptions_added,
        "expected_exceptions_removed": expected_exceptions_removed,
        "expected_exceptions_reason_updated": expected_exceptions_reason_updated,
        "resources_created": resources_created,
        "resources_pruned": resources_pruned,
        "resources_retained_crawled": resources_retained_crawled,
        "tools_pruned": tools_pruned,
        "tools_retained_crawled": tools_retained_crawled,
    }


def dry_run_ingest(gov: Governance, strict_drift: bool = False) -> GovernanceDiffReport:
    """Analyze the exact delta that would be applied without mutating anything."""
    with acquire_readonly_session() as session:
        check_backend_schema_version(session)
        res = queries.with_graph_state(
            lambda: _compute_dry_run_diff(session, gov, strict_drift),
            strict=True, state_reader=lambda: queries.graph_state(session),
        )
    report_fields = {f.name for f in GovernanceDiffReport.__dataclass_fields__.values()}
    kwargs = {k: v for k, v in res.items() if k in report_fields}
    return GovernanceDiffReport(**kwargs)


def governance_counts() -> dict[str, int]:
    """Authored-governance tallies in one UNION ALL statement."""
    query = """
        MATCH (n:Agent) RETURN 'agent' AS name, count(n) AS count
        UNION ALL
        MATCH (n:Policy) RETURN 'policy' AS name, count(n) AS count
        UNION ALL
        MATCH ()-[r:CAN_CALL]->() RETURN 'can_call' AS name, count(r) AS count
        UNION ALL
        MATCH ()-[r:READS]->() RETURN 'reads' AS name, count(r) AS count
        UNION ALL
        MATCH ()-[r:WRITES]->() RETURN 'writes' AS name, count(r) AS count
        UNION ALL
        MATCH ()-[r:GOVERNED_BY]->() RETURN 'governed_by' AS name, count(r) AS count
        UNION ALL
        MATCH (n:ExpectedException) RETURN 'expected_exception' AS name, count(n) AS count
    """
    names = (
        "agent",
        "policy",
        "can_call",
        "reads",
        "writes",
        "governed_by",
        "expected_exception",
    )
    with session() as s:
        counts = {r["name"]: r["count"] for r in s.run(query)}
    return {name: counts.get(name, 0) for name in names}

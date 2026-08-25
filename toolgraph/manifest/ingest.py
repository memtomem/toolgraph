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

from dataclasses import dataclass, field

from neo4j import ManagedTransaction

from toolgraph.graph.driver import session
from toolgraph.graph.queries import resolve_tool_keys
from toolgraph.graph.schema import bump_generation, exception_key
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


def _resolve(tx: ManagedTransaction, ref: str) -> tuple[str | None, list[str]]:
    """Return (unique_key, all_matches). unique_key is set only on exactly one match.

    Shares the exact resolution rule with check_access so write-side and
    read-side tool identity can never drift.
    """
    keys = resolve_tool_keys(tx, ref)
    return (keys[0] if len(keys) == 1 else None, keys)


def _resolve_problem(ref: str, matches: list[str]) -> str:
    return f"ambiguous {matches}" if matches else "not found"


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
    resolved_grants: list[tuple] = []
    for grant in gov.grants:
        key, matches = _resolve(tx, grant.tool)
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
        key, matches = _resolve(tx, da.tool)
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
            key, matches = _resolve(tx, node_ref)
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
        key, matches = _resolve(tx, exc.tool)
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

    if warnings:  # reject the whole manifest — do not mutate
        return warnings, notices

    # --- Phase 2: mutate (clean manifest only) ---------------------------
    tx.run("MATCH ()-[r:CAN_CALL|READS|WRITES|GOVERNED_BY]->() DELETE r")
    # ExpectedException nodes are pure authored bookkeeping — clear and re-apply
    # alongside the edges so a removed exception promotes the row back to violation.
    tx.run("MATCH (e:ExpectedException) DETACH DELETE e")

    tx.run("UNWIND $ids AS id MERGE (:Agent {id:id})", ids=sorted(known_agents))
    # Policy provenance lives on the GOVERNED_BY binding (per-edge), not the
    # node — see GovernedByBinding. Policy nodes carry only declaration data.
    # Null assignments drop the legacy ``prov_*`` properties left over from the
    # pre-completion-round shape, so an upgrading graph cleans itself up.
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

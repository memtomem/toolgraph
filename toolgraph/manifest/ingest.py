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

from neo4j import ManagedTransaction

from toolgraph.graph.driver import session
from toolgraph.graph.queries import resolve_tool_keys
from toolgraph.graph.schema import exception_key
from toolgraph.models import Governance, edge_provenance_props
from toolgraph.uris import is_resource_ref, normalize_resource_uri


def _resolve(tx: ManagedTransaction, ref: str) -> tuple[str | None, list[str]]:
    """Return (unique_key, all_matches). unique_key is set only on exactly one match.

    Shares the exact resolution rule with check_access so write-side and
    read-side tool identity can never drift.
    """
    keys = resolve_tool_keys(tx, ref)
    return (keys[0] if len(keys) == 1 else None, keys)


def _resolve_problem(ref: str, matches: list[str]) -> str:
    return f"ambiguous {matches}" if matches else "not found"


def _ingest(tx: ManagedTransaction, gov: Governance) -> list[str]:
    """Two phases in one transaction.

    Phase 1 validates every reference read-only and collects warnings. If ANY
    warning is found the manifest is rejected whole — nothing is mutated — so a
    typo can never wipe an existing DENY and leave a false ALLOW behind. Phase 2
    runs only for a fully clean manifest: clear authored edges, then re-apply.
    """
    warnings: list[str] = []
    known_policies = {p.id for p in gov.policies}

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
    for da in gov.data_access:
        key, matches = _resolve(tx, da.tool)
        if key is None:
            warnings.append(f"{da.mode}: tool {da.tool!r} {_resolve_problem(da.tool, matches)}")
        else:
            resolved_access.append(
                (key, da.mode, normalize_resource_uri(da.resource),
                 edge_provenance_props(da.provenance))
            )

    resolved_exceptions: list[tuple] = []  # (agent, tool_key, resource_or_None, policy, reason)
    for exc in gov.expected_exceptions:
        if exc.policy not in known_policies:
            warnings.append(
                f"EXPECTED_EXCEPTION: policy {exc.policy!r} not defined "
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
        resolved_exceptions.append(
            (
                exc.agent,
                key,
                normalize_resource_uri(exc.resource) if exc.resource else None,
                exc.policy,
                exc.reason,
            )
        )

    resolved_governed: list[tuple] = []  # (kind, key_or_uri, policy_ids)
    for node_ref, policy_ids in gov.governed_by.items():
        for pid in policy_ids:
            if pid not in known_policies:
                warnings.append(f"GOVERNED_BY: policy {pid!r} not defined (node {node_ref!r})")
        valid = [pid for pid in policy_ids if pid in known_policies]
        if is_resource_ref(node_ref):
            resolved_governed.append(("resource", normalize_resource_uri(node_ref), valid))
        else:
            key, matches = _resolve(tx, node_ref)
            if key is None:
                warnings.append(
                    f"GOVERNED_BY: tool {node_ref!r} {_resolve_problem(node_ref, matches)}"
                )
            else:
                resolved_governed.append(("tool", key, valid))

    if warnings:  # reject the whole manifest — do not mutate
        return warnings

    # --- Phase 2: mutate (clean manifest only) ---------------------------
    tx.run("MATCH ()-[r:CAN_CALL|READS|WRITES|GOVERNED_BY]-() DELETE r")
    # ExpectedException nodes are pure authored bookkeeping — clear and re-apply
    # alongside the edges so a removed exception promotes the row back to violation.
    tx.run("MATCH (e:ExpectedException) DETACH DELETE e")

    agents = set(gov.agents) | {g.agent for g in gov.grants}
    tx.run("UNWIND $ids AS id MERGE (:Agent {id:id})", ids=sorted(agents))
    # Policy provenance flattens onto the node so a reader sees source/evidence
    # alongside effect/scope without traversing an edge.
    policies_payload = []
    for p in gov.policies:
        d = p.model_dump(exclude={"provenance"})
        prov = edge_provenance_props(p.provenance)
        d["prov_source"] = prov["source"]
        d["prov_confidence"] = prov["confidence"]
        d["prov_evidence"] = prov["evidence"]
        policies_payload.append(d)
    tx.run(
        """
        UNWIND $policies AS p
        MERGE (pol:Policy {id:p.id})
        SET pol.effect=p.effect, pol.scope=p.scope, pol.description=p.description,
            pol.prov_source=p.prov_source, pol.prov_confidence=p.prov_confidence,
            pol.prov_evidence=p.prov_evidence
        """,
        policies=policies_payload,
    )

    for agent, key, granted_by, prov in resolved_grants:
        tx.run(
            """
            MERGE (a:Agent {id:$agent})
            WITH a MATCH (t:Tool {key:$key})
            MERGE (a)-[r:CAN_CALL]->(t)
            SET r.granted_by=$granted_by,
                r.source=$source, r.confidence=$confidence, r.evidence=$evidence
            """,
            agent=agent,
            key=key,
            granted_by=granted_by,
            **prov,
        )

    for key, mode, uri, prov in resolved_access:
        # mode is a validated Literal -> safe to inline as the relationship type.
        tx.run(
            f"""
            MATCH (t:Tool {{key:$key}})
            MERGE (res:Resource {{uri:$uri}})
            MERGE (t)-[r:{mode}]->(res)
            SET r.source=$source, r.confidence=$confidence, r.evidence=$evidence
            """,
            key=key,
            uri=uri,
            **prov,
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

    # GOVERNED_BY edges currently carry no per-edge provenance — the policy
    # node's prov_* fields cover the authoring intent. Default source on the
    # edge so queries can still filter uniformly.
    for kind, ref, pids in resolved_governed:
        if not pids:
            continue
        if kind == "resource":
            tx.run(
                """
                MERGE (res:Resource {uri:$ref})
                WITH res UNWIND $pids AS pid
                MATCH (p:Policy {id:pid})
                MERGE (res)-[r:GOVERNED_BY]->(p)
                SET r.source='operator_asserted', r.confidence='high'
                """,
                ref=ref,
                pids=pids,
            )
        else:
            tx.run(
                """
                MATCH (t:Tool {key:$ref})
                UNWIND $pids AS pid
                MATCH (p:Policy {id:pid})
                MERGE (t)-[r:GOVERNED_BY]->(p)
                SET r.source='operator_asserted', r.confidence='high'
                """,
                ref=ref,
                pids=pids,
            )

    return []


def ingest_governance(gov: Governance) -> list[str]:
    """Apply the manifest atomically; return warnings (graph untouched if non-empty)."""
    with session() as s:
        return s.execute_write(_ingest, gov)


def governance_counts() -> dict[str, int]:
    query = """
    CALL () { MATCH (a:Agent)  RETURN count(a) AS agent }
    CALL () { MATCH (p:Policy) RETURN count(p) AS policy }
    CALL () { MATCH ()-[c:CAN_CALL]->()    RETURN count(c) AS can_call }
    CALL () { MATCH ()-[r:READS]->()       RETURN count(r) AS reads }
    CALL () { MATCH ()-[w:WRITES]->()      RETURN count(w) AS writes }
    CALL () { MATCH ()-[g:GOVERNED_BY]->() RETURN count(g) AS governed_by }
    CALL () { MATCH (e:ExpectedException)  RETURN count(e) AS expected_exception }
    RETURN agent, policy, can_call, reads, writes, governed_by, expected_exception
    """
    with session() as s:
        rec = s.run(query).single()
        return dict(rec) if rec else {}

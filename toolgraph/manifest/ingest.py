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
from toolgraph.models import Governance
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
            resolved_grants.append((grant.agent, key, grant.granted_by))

    resolved_access: list[tuple] = []
    for da in gov.data_access:
        key, matches = _resolve(tx, da.tool)
        if key is None:
            warnings.append(f"{da.mode}: tool {da.tool!r} {_resolve_problem(da.tool, matches)}")
        else:
            resolved_access.append((key, da.mode, normalize_resource_uri(da.resource)))

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

    agents = set(gov.agents) | {g.agent for g in gov.grants}
    tx.run("UNWIND $ids AS id MERGE (:Agent {id:id})", ids=sorted(agents))
    tx.run(
        """
        UNWIND $policies AS p
        MERGE (pol:Policy {id:p.id})
        SET pol.effect=p.effect, pol.scope=p.scope, pol.description=p.description
        """,
        policies=[p.model_dump() for p in gov.policies],
    )

    for agent, key, granted_by in resolved_grants:
        tx.run(
            """
            MERGE (a:Agent {id:$agent})
            WITH a MATCH (t:Tool {key:$key})
            MERGE (a)-[r:CAN_CALL]->(t) SET r.granted_by=$granted_by
            """,
            agent=agent,
            key=key,
            granted_by=granted_by,
        )

    for key, mode, uri in resolved_access:
        # mode is a validated Literal -> safe to inline as the relationship type.
        tx.run(
            f"""
            MATCH (t:Tool {{key:$key}})
            MERGE (res:Resource {{uri:$uri}})
            MERGE (t)-[:{mode}]->(res)
            """,
            key=key,
            uri=uri,
        )

    for kind, ref, pids in resolved_governed:
        if not pids:
            continue
        if kind == "resource":
            tx.run(
                """
                MERGE (res:Resource {uri:$ref})
                WITH res UNWIND $pids AS pid
                MATCH (p:Policy {id:pid})
                MERGE (res)-[:GOVERNED_BY]->(p)
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
                MERGE (t)-[:GOVERNED_BY]->(p)
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
    RETURN agent, policy, can_call, reads, writes, governed_by
    """
    with session() as s:
        rec = s.run(query).single()
        return dict(rec) if rec else {}

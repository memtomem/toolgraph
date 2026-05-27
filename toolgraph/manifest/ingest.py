"""Ingest authored governance into the graph (declarative + idempotent).

The manifest is the single source of truth for authored edges: each ingest
first clears all CAN_CALL/READS/WRITES/GOVERNED_BY edges, then re-applies the
manifest. Revoking a grant in YAML therefore removes it from the graph. Crawl-
owned EXPOSES/PROVIDES edges are untouched.

Tool references resolve to a unique key ("<server>::<tool>" exact, or a bare
name when it matches exactly one tool). Bare names matching multiple servers,
unknown tools, and undefined policy ids are reported as warnings rather than
silently producing wrong edges (a false ALLOW is the dangerous failure mode).
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
    warnings: list[str] = []
    known_policies = {p.id for p in gov.policies}

    # Declarative: clear authored edges (crawl-owned EXPOSES/PROVIDES kept).
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

    for grant in gov.grants:
        key, matches = _resolve(tx, grant.tool)
        if key is None:
            warnings.append(
                f"CAN_CALL: tool {grant.tool!r} {_resolve_problem(grant.tool, matches)} "
                f"(agent {grant.agent!r})"
            )
            continue
        tx.run(
            """
            MERGE (a:Agent {id:$agent})
            WITH a MATCH (t:Tool {key:$key})
            MERGE (a)-[r:CAN_CALL]->(t) SET r.granted_by=$granted_by
            """,
            agent=grant.agent,
            key=key,
            granted_by=grant.granted_by,
        )

    for da in gov.data_access:
        key, matches = _resolve(tx, da.tool)
        if key is None:
            warnings.append(f"{da.mode}: tool {da.tool!r} {_resolve_problem(da.tool, matches)}")
            continue
        # da.mode is a validated Literal -> safe to inline as the relationship type.
        tx.run(
            f"""
            MATCH (t:Tool {{key:$key}})
            MERGE (res:Resource {{uri:$uri}})
            MERGE (t)-[:{da.mode}]->(res)
            """,
            key=key,
            uri=normalize_resource_uri(da.resource),
        )

    for node_ref, policy_ids in gov.governed_by.items():
        for pid in policy_ids:
            if pid not in known_policies:
                warnings.append(f"GOVERNED_BY: policy {pid!r} not defined (node {node_ref!r})")
        valid = [pid for pid in policy_ids if pid in known_policies]
        if not valid:
            continue

        if is_resource_ref(node_ref):
            tx.run(
                """
                MERGE (res:Resource {uri:$ref})
                WITH res UNWIND $pids AS pid
                MATCH (p:Policy {id:pid})
                MERGE (res)-[:GOVERNED_BY]->(p)
                """,
                ref=normalize_resource_uri(node_ref),
                pids=valid,
            )
        else:
            key, matches = _resolve(tx, node_ref)
            if key is None:
                warnings.append(
                    f"GOVERNED_BY: tool {node_ref!r} {_resolve_problem(node_ref, matches)}"
                )
                continue
            tx.run(
                """
                MATCH (t:Tool {key:$key})
                UNWIND $pids AS pid
                MATCH (p:Policy {id:pid})
                MERGE (t)-[:GOVERNED_BY]->(p)
                """,
                key=key,
                pids=valid,
            )

    return warnings


def ingest_governance(gov: Governance) -> list[str]:
    """Apply the manifest declaratively; return warnings for unresolved refs/policies."""
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

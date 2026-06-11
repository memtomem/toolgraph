"""Load factual crawl results into the graph (idempotent).

Only crawled facts land here: MCPServer/Tool/Resource nodes and the
EXPOSES/PROVIDES edges. Authored governance edges are handled by manifest.ingest.

Idempotency: every node is MERGEd on its unique key (server name, tool
``key``, resource uri), so re-crawling identical data never duplicates.
``input_schema`` is JSON-serialized because Neo4j properties cannot hold nested
maps.
"""

from __future__ import annotations

import json

from neo4j import ManagedTransaction

from toolgraph.graph.driver import session
from toolgraph.graph.schema import BUMP_GENERATION, tool_key
from toolgraph.models import CrawlResult
from toolgraph.uris import normalize_resource_uri


def _merge_crawl(tx: ManagedTransaction, result: CrawlResult) -> list[str]:
    tx.run(
        """
        MERGE (s:MCPServer {name:$name})
        SET s.version=$version, s.transport=$transport,
            s.endpoint=$endpoint, s.last_crawled_at=datetime()
        """,
        name=result.server_name,
        version=result.server_version,
        transport=result.transport,
        endpoint=result.endpoint,
    )

    tools = [
        {
            "key": tool_key(result.server_name, t.name),
            "name": t.name,
            "description": t.description,
            "input_schema": json.dumps(t.input_schema) if t.input_schema is not None else None,
        }
        for t in result.tools
    ]
    # EXPOSES carries crawled provenance: source/confidence/evidence so a reader
    # can distinguish facts (here) from operator assertions (CAN_CALL etc.).
    tx.run(
        """
        MATCH (s:MCPServer {name:$name})
        UNWIND $tools AS t
        MERGE (tool:Tool {key:t.key})
        SET tool.name=t.name, tool.server=$name,
            tool.description=t.description, tool.input_schema=t.input_schema
        MERGE (s)-[e:EXPOSES]->(tool)
        SET e.source='crawled', e.confidence='high', e.evidence=$evidence
        """,
        name=result.server_name,
        tools=tools,
        evidence=f"crawled from {result.endpoint or result.transport}",
    )

    resources = [
        {
            "uri": normalize_resource_uri(r.uri),
            "name": r.name,
            "mime_type": r.mime_type,
            "description": r.description,
        }
        for r in result.resources
    ]
    tx.run(
        """
        MATCH (s:MCPServer {name:$name})
        UNWIND $resources AS r
        MERGE (res:Resource {uri:r.uri})
        SET res.name=r.name, res.mime_type=r.mime_type, res.description=r.description
        MERGE (s)-[p:PROVIDES]->(res)
        SET p.source='crawled', p.confidence='high', p.evidence=$evidence
        """,
        name=result.server_name,
        resources=resources,
        evidence=f"crawled from {result.endpoint or result.transport}",
    )

    # Reconcile tools this server no longer exposes. Drop the EXPOSES edge always;
    # DETACH DELETE only pure crawl artifacts (no authored governance). Tools that
    # still carry authored CAN_CALL/READS/WRITES/GOVERNED_BY are KEPT — silently
    # deleting them would drop a DENY and create a false ALLOW — and surfaced as a
    # warning to re-run ingest-manifest.
    keys = [t["key"] for t in tools]
    tx.run(
        "MATCH (s:MCPServer {name:$name})-[e:EXPOSES]->(old:Tool) "
        "WHERE NOT old.key IN $keys DELETE e",
        name=result.server_name,
        keys=keys,
    )
    tx.run(
        """
        MATCH (old:Tool {server:$name})
        WHERE NOT old.key IN $keys
          AND NOT EXISTS { MATCH (old)-[:CAN_CALL|READS|WRITES|GOVERNED_BY]-() }
        DETACH DELETE old
        """,
        name=result.server_name,
        keys=keys,
    )
    rec = tx.run(
        "MATCH (old:Tool {server:$name}) WHERE NOT old.key IN $keys "
        "RETURN collect(old.key) AS orphaned",
        name=result.server_name,
        keys=keys,
    ).single()
    orphaned = rec["orphaned"] if rec else []

    # PROVIDES: drop edges to resources no longer advertised; keep the (shared,
    # possibly governed) Resource node itself.
    tx.run(
        """
        MATCH (s:MCPServer {name:$name})-[p:PROVIDES]->(old:Resource)
        WHERE NOT old.uri IN $uris
        DELETE p
        """,
        name=result.server_name,
        uris=[r["uri"] for r in resources],
    )

    # ADR-0004: same transaction as the mutation, so a reader can never see
    # new graph state under an old generation (or vice versa).
    tx.run(BUMP_GENERATION)

    return [
        f"tool {k!r} no longer exposed by {result.server_name!r} but retains authored "
        f"governance (kept so a DENY cannot silently become ALLOW; remove it from the "
        f"manifest and re-ingest — re-ingesting unchanged just re-targets the stale "
        f"tool and emits a DRIFT notice)"
        for k in orphaned
    ]


def load_crawl_result(result: CrawlResult) -> list[str]:
    """Upsert one server's crawl into the graph; return reconciliation warnings."""
    with session() as s:
        return s.execute_write(_merge_crawl, result)


def graph_counts() -> dict[str, int]:
    """Node/edge tallies — used by the idempotency check and the demo."""
    query = """
    CALL () {  MATCH (s:MCPServer) RETURN count(s) AS mcpserver }
    CALL () {  MATCH (t:Tool)      RETURN count(t) AS tool }
    CALL () {  MATCH (r:Resource)  RETURN count(r) AS resource }
    CALL () {  MATCH ()-[e:EXPOSES]->()  RETURN count(e) AS exposes }
    CALL () {  MATCH ()-[p:PROVIDES]->() RETURN count(p) AS provides }
    RETURN mcpserver, tool, resource, exposes, provides
    """
    with session() as s:
        rec = s.run(query).single()
        return dict(rec) if rec else {}

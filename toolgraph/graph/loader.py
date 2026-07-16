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
from toolgraph.graph.schema import bump_generation, tool_key
from toolgraph.models import CrawlResult
from toolgraph.redaction import persisted_endpoint
from toolgraph.uris import normalize_resource_uri


def _merge_crawl(tx: ManagedTransaction, result: CrawlResult) -> list[str]:
    endpoint = persisted_endpoint(result.transport, result.endpoint)
    tx.run(
        """
        MERGE (s:MCPServer {name:$name})
        SET s.version=$version, s.transport=$transport,
            s.endpoint=$endpoint, s.last_crawled_at=datetime()
        """,
        name=result.server_name,
        version=result.server_version,
        transport=result.transport,
        endpoint=endpoint,
    )

    tools = [
        {
            "key": tool_key(result.server_name, t.name),
            "name": t.name,
            "description": t.description,
            "input_schema": json.dumps(t.input_schema) if t.input_schema is not None else None,
            "read_only_hint": t.read_only_hint,
            "destructive_hint": t.destructive_hint,
            "idempotent_hint": t.idempotent_hint,
            "open_world_hint": t.open_world_hint,
        }
        for t in result.tools
    ]
    # EXPOSES carries crawled provenance: source/confidence/evidence so a reader
    # can distinguish facts (here) from operator assertions (CAN_CALL etc.).
    # Annotation hints (ADR-0006) are SET unconditionally: a null value removes
    # the property, so a hint the server stopped sending is cleared on the same
    # crawl pass that owns the node (same lifecycle as description).
    tx.run(
        """
        MATCH (s:MCPServer {name:$name})
        UNWIND $tools AS t
        MERGE (tool:Tool {key:t.key})
        SET tool.name=t.name, tool.server=$name,
            tool.description=t.description, tool.input_schema=t.input_schema,
            tool.read_only_hint=t.read_only_hint,
            tool.destructive_hint=t.destructive_hint,
            tool.idempotent_hint=t.idempotent_hint,
            tool.open_world_hint=t.open_world_hint
        MERGE (s)-[e:EXPOSES]->(tool)
        SET e.source='crawled', e.confidence='high', e.evidence=$evidence
        """,
        name=result.server_name,
        tools=tools,
        evidence=f"crawled from {endpoint}",
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
        evidence=f"crawled from {endpoint}",
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
    # A tool the server stopped exposing makes NO annotation claims anymore —
    # the node may be kept below (authored governance), but leaving the hints
    # would have the audit queries cite a crawled/medium claim nobody is
    # making (ADR-0006; Codex review of this branch caught it).
    tx.run(
        """
        MATCH (old:Tool {server:$name})
        WHERE NOT old.key IN $keys
        SET old.read_only_hint=null, old.destructive_hint=null,
            old.idempotent_hint=null, old.open_world_hint=null
        """,
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
    orphaned = (rec["orphaned"] if rec else None) or []

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
    bump_generation(tx)

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


def _retire_unlisted(tx: ManagedTransaction, keep: list[str]) -> tuple[list[str], list[str]]:
    rec = tx.run(
        "MATCH (s:MCPServer) WHERE NOT s.name IN $keep "
        "RETURN collect(s.name) AS retired",
        keep=keep,
    ).single()
    retired = sorted((rec["retired"] if rec else None) or [])
    if not retired:
        # Nothing changed — don't bump the generation (ADR-0004: the token
        # invalidates caches; a no-op reconciliation must leave them valid).
        return [], []

    # Resources these servers provided: candidates for deletion once the
    # PROVIDES edges drop with the server node below.
    rec = tx.run(
        "MATCH (s:MCPServer)-[:PROVIDES]->(r:Resource) WHERE s.name IN $names "
        "RETURN collect(DISTINCT r.uri) AS uris",
        names=retired,
    ).single()
    resource_uris = (rec["uris"] if rec else None) or []

    # A retired server makes no annotation claims anymore (ADR-0006) — kept
    # tools must not keep citing crawled/medium self-claims from a server
    # that no longer exists.
    tx.run(
        """
        MATCH (t:Tool) WHERE t.server IN $names
        SET t.read_only_hint=null, t.destructive_hint=null,
            t.idempotent_hint=null, t.open_world_hint=null
        """,
        names=retired,
    )
    # Same keep-rule as per-server reconciliation: tools with authored
    # governance are kept (a silent delete would turn a DENY into a false
    # ALLOW); pure crawl artifacts go.
    tx.run(
        """
        MATCH (t:Tool) WHERE t.server IN $names
          AND NOT EXISTS { MATCH (t)-[:CAN_CALL|READS|WRITES|GOVERNED_BY]-() }
        DETACH DELETE t
        """,
        names=retired,
    )
    rec = tx.run(
        "MATCH (t:Tool) WHERE t.server IN $names RETURN collect(t.key) AS kept",
        names=retired,
    ).single()
    kept = sorted((rec["kept"] if rec else None) or [])

    # The server node and all its EXPOSES/PROVIDES edges go together — kept
    # tools lose their EXPOSES here and become honestly drifted.
    tx.run(
        "MATCH (s:MCPServer) WHERE s.name IN $names DETACH DELETE s",
        names=retired,
    )
    # ADR-0001 resource rule (same degree-0 filter as ingest Phase 2): a
    # resource nobody provides and nothing authored references is a crawl
    # artifact of a dead server. Shared or governed resources keep edges
    # and never match.
    tx.run(
        """
        MATCH (r:Resource) WHERE r.uri IN $uris
          AND NOT EXISTS { MATCH (r)--() }
        DELETE r
        """,
        uris=resource_uris,
    )

    bump_generation(tx)
    return retired, [
        f"tool {k!r} kept after its server was retired from servers.yaml — it "
        f"retains authored governance (kept so a DENY cannot silently become "
        f"ALLOW; it is now drifted — remove it from the manifest and re-ingest "
        f"to retire it)"
        for k in kept
    ]


def retire_unlisted_servers(keep: set[str]) -> tuple[list[str], list[str]]:
    """Fleet reconciliation (ADR-0007): retire crawled subgraphs of servers
    no longer in servers.yaml. Returns (retired server names, warnings)."""
    with session() as s:
        return s.execute_write(_retire_unlisted, sorted(keep))


def graph_counts() -> dict[str, int]:
    """Node/edge tallies — used by the idempotency check and the demo."""
    queries = {
        "mcpserver": "MATCH (n:MCPServer) RETURN count(n) AS count",
        "tool": "MATCH (n:Tool) RETURN count(n) AS count",
        "resource": "MATCH (n:Resource) RETURN count(n) AS count",
        "exposes": "MATCH ()-[r:EXPOSES]->() RETURN count(r) AS count",
        "provides": "MATCH ()-[r:PROVIDES]->() RETURN count(r) AS count",
    }
    with session() as s:
        return {
            name: (record["count"] if (record := s.run(query).single()) else 0)
            for name, query in queries.items()
        }

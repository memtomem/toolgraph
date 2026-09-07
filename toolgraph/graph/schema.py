"""Graph schema: uniqueness constraints.

Single-property uniqueness only — these work on Neo4j Community (node-key and
composite-key constraints are Enterprise-only). Tools therefore carry a synthetic
``key`` (``"<server>::<tool>"``) so identity is a single indexed property and
MERGE stays idempotent. No vector index in this MVP.
"""

from __future__ import annotations

from uuid import uuid4

from toolgraph.graph.driver import BackendConfigurationError, backend_name, session

# Version of the physical backend schema this code understands. Bumped only
# when a table/constraint shape changes incompatibly. IF NOT EXISTS silently
# skips outdated tables, so without this gate an old database meets new code
# only as confusing query-time errors.
BACKEND_SCHEMA_VERSION = 1

CONSTRAINTS: list[str] = [
    "CREATE CONSTRAINT mcpserver_name IF NOT EXISTS FOR (s:MCPServer) REQUIRE s.name IS UNIQUE",
    "CREATE CONSTRAINT tool_key IF NOT EXISTS FOR (t:Tool) REQUIRE t.key IS UNIQUE",
    "CREATE CONSTRAINT resource_uri IF NOT EXISTS FOR (r:Resource) REQUIRE r.uri IS UNIQUE",
    "CREATE CONSTRAINT agent_id IF NOT EXISTS FOR (a:Agent) REQUIRE a.id IS UNIQUE",
    "CREATE CONSTRAINT policy_id IF NOT EXISTS FOR (p:Policy) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT exception_key IF NOT EXISTS FOR (e:ExpectedException) REQUIRE e.key IS UNIQUE",
    "CREATE CONSTRAINT graphmeta_id IF NOT EXISTS FOR (m:GraphMeta) REQUIRE m.id IS UNIQUE",
]

# ADR-0004: run in the SAME transaction as every successful crawl load and
# manifest ingest, so generation and graph state can never be observed out of
# sync. It is an invalidation token, not a semantic version — a crawl of N
# servers bumps N times. Rejected ingests return before reaching this.
BUMP_GENERATION = (
    "MERGE (m:GraphMeta {id:'singleton'}) "
    "SET m.instance_id = coalesce(m.instance_id, $graph_instance_id), "
    "m.generation = coalesce(m.generation, 0) + 1, m.updated_at = datetime()"
)

ENSURE_GRAPH_META = (
    "MERGE (m:GraphMeta {id:'singleton'}) "
    "SET m.instance_id = coalesce(m.instance_id, $graph_instance_id), "
    "m.generation = coalesce(m.generation, 0)"
)

LADYBUG_SCHEMA: list[str] = [
    "CREATE NODE TABLE IF NOT EXISTS MCPServer("
    "name STRING PRIMARY KEY, version STRING, transport STRING, endpoint STRING, "
    "last_crawled_at TIMESTAMP)",
    "CREATE NODE TABLE IF NOT EXISTS Tool("
    "key STRING PRIMARY KEY, name STRING, server STRING, description STRING, "
    "input_schema STRING, read_only_hint BOOL, destructive_hint BOOL, "
    "idempotent_hint BOOL, open_world_hint BOOL)",
    "CREATE NODE TABLE IF NOT EXISTS Resource("
    "uri STRING PRIMARY KEY, name STRING, mime_type STRING, description STRING)",
    "CREATE NODE TABLE IF NOT EXISTS Agent(id STRING PRIMARY KEY)",
    "CREATE NODE TABLE IF NOT EXISTS Policy("
    "id STRING PRIMARY KEY, effect STRING, scope STRING, description STRING, "
    "prov_source STRING, prov_confidence STRING, prov_evidence STRING)",
    "CREATE NODE TABLE IF NOT EXISTS ExpectedException("
    "key STRING PRIMARY KEY, agent STRING, tool_key STRING, resource STRING, "
    "policy STRING, reason STRING)",
    "CREATE NODE TABLE IF NOT EXISTS GraphMeta("
    "id STRING PRIMARY KEY, instance_id STRING, generation INT64, updated_at TIMESTAMP, "
    "governance_digest STRING, last_policy_change_id STRING, backend_schema_version INT64)",
    "CREATE REL TABLE IF NOT EXISTS EXPOSES("
    "FROM MCPServer TO Tool, source STRING, confidence STRING, evidence STRING)",
    "CREATE REL TABLE IF NOT EXISTS PROVIDES("
    "FROM MCPServer TO Resource, source STRING, confidence STRING, evidence STRING)",
    "CREATE REL TABLE IF NOT EXISTS CAN_CALL("
    "FROM Agent TO Tool, granted_by STRING, source STRING, confidence STRING, evidence STRING)",
    "CREATE REL TABLE IF NOT EXISTS READS("
    "FROM Tool TO Resource, source STRING, confidence STRING, evidence STRING)",
    "CREATE REL TABLE IF NOT EXISTS WRITES("
    "FROM Tool TO Resource, source STRING, confidence STRING, evidence STRING)",
    "CREATE REL TABLE IF NOT EXISTS GOVERNED_BY("
    "FROM Resource TO Policy, FROM Tool TO Policy, "
    "source STRING, confidence STRING, evidence STRING)",
]


def exception_key(agent: str, tool_key: str, resource: str | None, policy: str) -> str:
    """Collision-safe identity; null denotes an exception for any resource."""
    from toolgraph.artifacts import canonical_json_bytes, sha256_bytes
    from toolgraph.uris import normalize_resource_uri

    resource = normalize_resource_uri(resource) if resource is not None else None
    return "v2:" + sha256_bytes(canonical_json_bytes([agent, tool_key, resource, policy]))


def tool_key(server_name: str, tool_name: str) -> str:
    """Stable identity for a Tool node.

    Components must not contain the delimiter: (server="a", tool="b::c") and
    (server="a::b", tool="c") would otherwise collapse onto one key and MERGE
    the same node. Model validation enforces this at the crawl boundary; this
    check covers programmatic callers.
    """
    if "::" in server_name or "::" in tool_name:
        raise ValueError(
            "server and tool names must not contain '::' (the tool-key delimiter)"
        )
    return f"{server_name}::{tool_name}"


def _stored_backend_schema_version(s) -> int | None:
    try:
        record = s.run(
            "MATCH (m:GraphMeta {id:'singleton'}) "
            "RETURN m.backend_schema_version AS version"
        ).single()
    except RuntimeError:
        # Fresh embedded database: the GraphMeta table does not exist yet.
        return None
    return record["version"] if record else None


def _refuse_version_mismatch(stored: int | None) -> None:
    if stored is not None and stored != BACKEND_SCHEMA_VERSION:
        raise BackendConfigurationError(
            f"backend schema version {stored} does not match this build's "
            f"{BACKEND_SCHEMA_VERSION} — "
            + (
                "upgrade toolgraph to a version that understands it"
                if stored > BACKEND_SCHEMA_VERSION
                else "migrate the database (no automatic migration exists)"
            )
        )


def init_schema() -> None:
    """Create all constraints (idempotent) and gate on the schema version."""
    with session() as s:
        # Gate BEFORE running any version-specific DDL: an incompatible client
        # must not mutate the database on its way to the mismatch error.
        _refuse_version_mismatch(_stored_backend_schema_version(s))
        for stmt in LADYBUG_SCHEMA if backend_name() == "ladybug" else CONSTRAINTS:
            s.run(stmt)
        # Backfill databases created before the graph-state contract without
        # invalidating caches: no authored or crawled graph fact changed.
        s.run(ENSURE_GRAPH_META, graph_instance_id=str(uuid4()))
        # Stamp without clobbering: coalesce keeps whatever version a
        # concurrent initializer already wrote; every shipped database has the
        # v1 shape, so a null stamp is always safe to fill.
        s.run(
            "MATCH (m:GraphMeta {id:'singleton'}) "
            "SET m.backend_schema_version = "
            "coalesce(m.backend_schema_version, $version)",
            version=BACKEND_SCHEMA_VERSION,
        )
        # Re-read: if a different-version initializer won the coalesce race,
        # refuse now instead of proceeding against its database.
        _refuse_version_mismatch(_stored_backend_schema_version(s))


def list_constraints() -> list[dict]:
    if backend_name() == "ladybug":
        return [{"name": statement.split("EXISTS", 1)[1].split("(", 1)[0].strip()}
                for statement in LADYBUG_SCHEMA]
    with session() as s:
        return [dict(r) for r in s.run("SHOW CONSTRAINTS")]


def bump_generation(tx) -> None:
    tx.run(BUMP_GENERATION, graph_instance_id=str(uuid4()))

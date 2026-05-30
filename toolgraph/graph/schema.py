"""Graph schema: uniqueness constraints.

Single-property uniqueness only — these work on Neo4j Community (node-key and
composite-key constraints are Enterprise-only). Tools therefore carry a synthetic
``key`` (``"<server>::<tool>"``) so identity is a single indexed property and
MERGE stays idempotent. No vector index in this MVP.
"""

from __future__ import annotations

from toolgraph.graph.driver import session

CONSTRAINTS: list[str] = [
    "CREATE CONSTRAINT mcpserver_name IF NOT EXISTS FOR (s:MCPServer) REQUIRE s.name IS UNIQUE",
    "CREATE CONSTRAINT tool_key IF NOT EXISTS FOR (t:Tool) REQUIRE t.key IS UNIQUE",
    "CREATE CONSTRAINT resource_uri IF NOT EXISTS FOR (r:Resource) REQUIRE r.uri IS UNIQUE",
    "CREATE CONSTRAINT agent_id IF NOT EXISTS FOR (a:Agent) REQUIRE a.id IS UNIQUE",
    "CREATE CONSTRAINT policy_id IF NOT EXISTS FOR (p:Policy) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT exception_key IF NOT EXISTS FOR (e:ExpectedException) REQUIRE e.key IS UNIQUE",
]


def exception_key(agent: str, tool_key: str, resource: str | None, policy: str) -> str:
    """Stable identity for ExpectedException; ``resource=*`` collapses 'any resource'."""
    return f"{agent}|{tool_key}|{resource or '*'}|{policy}"


def tool_key(server_name: str, tool_name: str) -> str:
    """Stable identity for a Tool node."""
    return f"{server_name}::{tool_name}"


def init_schema() -> None:
    """Create all constraints (idempotent)."""
    with session() as s:
        for stmt in CONSTRAINTS:
            s.run(stmt)


def list_constraints() -> list[dict]:
    with session() as s:
        return [dict(r) for r in s.run("SHOW CONSTRAINTS")]

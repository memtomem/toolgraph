"""The three governance queries — the product.

Each returns explainable structure (the path + evidence), not a bare boolean.
Reachability spans Agent -> Tool -> Resource -> Policy across four node types,
which a vector index cannot express. Shared by the CLI and the MCP server.

A tool can be denied two ways, both honored here:
- transitively: Tool -[:READS|WRITES]-> Resource -[:GOVERNED_BY]-> Policy(DENY)
- directly:     Tool -[:GOVERNED_BY]-> Policy(DENY)

Tool refs accept the exact key ("<server>::<tool>") or a bare name. A bare name
that matches tools on multiple servers is reported as AMBIGUOUS_TOOL rather than
silently resolved. ``blast_radius`` auto-detects a resource URI (contains "://")
vs a policy id.
"""

from __future__ import annotations

from neo4j import Session

from toolgraph.graph.driver import session
from toolgraph.uris import is_resource_ref, normalize_resource_uri


def resolve_tool_keys(s: Session, ref: str) -> list[str]:
    """All Tool keys matching a ref (exact key, or bare name across servers)."""
    rows = s.run(
        "MATCH (t:Tool) WHERE t.key = $ref OR (NOT $ref CONTAINS '::' AND t.name = $ref) "
        "RETURN t.key AS key ORDER BY key",
        ref=ref,
    )
    return [r["key"] for r in rows]


def _resource_path(tool: str, mode: str, resource: str, policy: str) -> str:
    return f"({tool}) -{mode}-> ({resource}) -GOVERNED_BY-> ({policy}:DENY)"


def _tool_path(tool: str, policy: str) -> str:
    return f"({tool}) -GOVERNED_BY-> ({policy}:DENY)"


def _radius_path(agent: str | None, tool: str | None, mode: str | None, resource: str) -> str | None:
    if agent and tool:
        return f"({agent}) -CAN_CALL-> ({tool}) -{mode}-> ({resource})"
    if tool:
        return f"({tool}) -{mode}-> ({resource})"
    return None


def _deny_evidence_for(s: Session, key: str) -> list[dict]:
    """All ways the tool identified by `key` reaches a DENY policy."""
    evidence: list[dict] = []
    for r in s.run(
        """
        MATCH (t:Tool {key:$key})-[acc:READS|WRITES]->(r:Resource)-[:GOVERNED_BY]->(p:Policy {effect:'DENY'})
        RETURN DISTINCT type(acc) AS mode, r.uri AS resource, p.id AS policy
        ORDER BY resource, policy
        """,
        key=key,
    ):
        evidence.append(
            {
                "via": "resource",
                "mode": r["mode"],
                "resource": r["resource"],
                "policy": r["policy"],
                "path": _resource_path(_name(key), r["mode"], r["resource"], r["policy"]),
            }
        )
    for r in s.run(
        """
        MATCH (t:Tool {key:$key})-[:GOVERNED_BY]->(p:Policy {effect:'DENY'})
        RETURN DISTINCT p.id AS policy ORDER BY policy
        """,
        key=key,
    ):
        evidence.append(
            {"via": "tool", "policy": r["policy"], "path": _tool_path(_name(key), r["policy"])}
        )
    return evidence


def _name(key: str) -> str:
    return key.split("::", 1)[1] if "::" in key else key


def check_access(agent: str, tool: str) -> dict:
    """Can the agent call the tool, and does the call cross a DENY policy?

    verdict: TOOL_NOT_FOUND | AMBIGUOUS_TOOL | NOT_GRANTED | DENY | ALLOW.
    """
    with session() as s:
        keys = resolve_tool_keys(s, tool)
        if not keys:
            return {"agent": agent, "tool": tool, "found": False, "verdict": "TOOL_NOT_FOUND"}
        if len(keys) > 1:
            return {
                "agent": agent,
                "tool": tool,
                "found": True,
                "verdict": "AMBIGUOUS_TOOL",
                "candidates": keys,
            }

        key = keys[0]
        meta = s.run(
            """
            MATCH (t:Tool {key:$key})
            RETURN t.name AS name, t.server AS server,
                   EXISTS { MATCH (:Agent {id:$agent})-[:CAN_CALL]->(t) } AS permitted
            """,
            key=key,
            agent=agent,
        ).single()
        if meta is None:  # tool deleted between resolve and this read
            return {"agent": agent, "tool": tool, "found": False, "verdict": "TOOL_NOT_FOUND"}
        deny = _deny_evidence_for(s, key)

    if not meta["permitted"]:
        verdict = "NOT_GRANTED"
    elif deny:
        verdict = "DENY"
    else:
        verdict = "ALLOW"

    return {
        "agent": agent,
        "tool": meta["name"],
        "tool_key": key,
        "server": meta["server"],
        "found": True,
        "permitted": meta["permitted"],
        "verdict": verdict,
        "deny_evidence": deny,
    }


def unsafe_callable_tools(agent: str) -> list[dict]:
    """Tools the agent can call that reach a DENY policy (via resource or directly)."""
    rows: list[dict] = []
    with session() as s:
        for r in s.run(
            """
            MATCH (:Agent {id:$agent})-[:CAN_CALL]->(t:Tool)
                  -[acc:READS|WRITES]->(res:Resource)-[:GOVERNED_BY]->(p:Policy {effect:'DENY'})
            RETURN DISTINCT t.key AS tool_key, t.name AS tool, t.server AS server,
                   'resource' AS via, type(acc) AS mode, res.uri AS resource, p.id AS policy
            ORDER BY tool, resource
            """,
            agent=agent,
        ):
            d = dict(r)
            d["path"] = _resource_path(d["tool"], d["mode"], d["resource"], d["policy"])
            rows.append(d)
        for r in s.run(
            """
            MATCH (:Agent {id:$agent})-[:CAN_CALL]->(t:Tool)-[:GOVERNED_BY]->(p:Policy {effect:'DENY'})
            RETURN DISTINCT t.key AS tool_key, t.name AS tool, t.server AS server,
                   'tool' AS via, null AS mode, null AS resource, p.id AS policy
            ORDER BY tool
            """,
            agent=agent,
        ):
            d = dict(r)
            d["path"] = _tool_path(d["tool"], d["policy"])
            rows.append(d)
    return rows


def blast_radius(node: str) -> dict:
    """Who/what is affected if a resource or a policy changes (with evidence paths).

    Every impacted row has the same keys (via/resource/agent/tool_key/tool/mode/path);
    `path` is None when a governed node is reachable by no agent. A tool that touches
    the resource but has no caller is still reported (it is affected).
    """
    if is_resource_ref(node):
        uri = normalize_resource_uri(node)
        with session() as s:
            impacted = []
            for r in s.run(
                """
                MATCH (r:Resource {uri:$uri})
                OPTIONAL MATCH (t:Tool)-[acc:READS|WRITES]->(r)
                OPTIONAL MATCH (a:Agent)-[:CAN_CALL]->(t)
                RETURN DISTINCT a.id AS agent, t.key AS tool_key, t.name AS tool, type(acc) AS mode
                ORDER BY agent, tool
                """,
                uri=uri,
            ):
                if r["tool"] is None:
                    continue  # resource exists but nothing reads/writes it
                d = dict(r)
                d["path"] = _radius_path(d["agent"], d["tool"], d["mode"], uri)
                impacted.append(d)
        return {"kind": "resource", "node": uri, "impacted": impacted}

    with session() as s:
        impacted = []
        # resources this policy governs, and who reaches them
        for r in s.run(
            """
            MATCH (res:Resource)-[:GOVERNED_BY]->(:Policy {id:$node})
            OPTIONAL MATCH (a:Agent)-[:CAN_CALL]->(t:Tool)-[acc:READS|WRITES]->(res)
            RETURN 'resource' AS via, res.uri AS resource, a.id AS agent,
                   t.key AS tool_key, t.name AS tool, type(acc) AS mode
            ORDER BY resource, agent, tool
            """,
            node=node,
        ):
            d = dict(r)
            d["path"] = _radius_path(d["agent"], d["tool"], d["mode"], d["resource"])
            impacted.append(d)
        # tools this policy governs directly, and who can call them
        for r in s.run(
            """
            MATCH (t:Tool)-[:GOVERNED_BY]->(:Policy {id:$node})
            OPTIONAL MATCH (a:Agent)-[:CAN_CALL]->(t)
            RETURN 'tool' AS via, null AS resource, a.id AS agent,
                   t.key AS tool_key, t.name AS tool, null AS mode
            ORDER BY tool, agent
            """,
            node=node,
        ):
            d = dict(r)
            d["path"] = (
                f"({d['agent']}) -CAN_CALL-> ({d['tool']}) [GOVERNED_BY {node}]"
                if d["agent"]
                else None
            )
            impacted.append(d)
    return {"kind": "policy", "node": node, "impacted": impacted}

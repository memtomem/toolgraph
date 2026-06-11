"""toolgraph as an MCP server: agents query the registry over the same protocol
it crawls. Thin wrappers over graph.queries — all logic lives there.

Every response carries ``graph_generation`` (ADR-0004) so callers can cache
graph-derived features and pin telemetry to a replayable graph state. The
stamp lives HERE, not in the query layer — CLI output stays unchanged.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from toolgraph.graph import queries

mcp = FastMCP("toolgraph")


@mcp.tool()
def check_access(agent: str, tool: str) -> dict:
    """Can `agent` call `tool`, and does the call path cross a DENY policy?

    Returns a verdict (AGENT_NOT_FOUND | TOOL_NOT_FOUND | AMBIGUOUS_TOOL |
    NOT_GRANTED | DENY | ALLOW) with the exact resource/policy evidence path.
    Every response carries ``agent_found: bool`` and ``found`` for the tool
    (True/False when resolution ran; null on AGENT_NOT_FOUND). On DENY, the
    response also carries ``all_authorized: bool`` — True when every
    deny_evidence row is an operator-declared exception.
    """
    result = queries.check_access(agent, tool)
    result["graph_generation"] = queries.graph_generation()
    return result


@mcp.tool()
def unsafe_callable_tools(agent: str, include_authorized: bool = False) -> dict:
    """Tools `agent` is allowed to call that reach a DENY policy.

    Returns an object so the MCP structured-content shape is stable:
    ``{"agent", "agent_found", "count", "tools": [...]}``.

    Defaults to ``classification='violation'`` rows only — matching the CLI
    semantics so MCP consumers don't see authorized_but_governed rows mixed
    in by default. Pass ``include_authorized=True`` for the full list.

    ``agent_found=False`` distinguishes a typo'd agent (returns empty) from
    an agent with zero unsafe tools (also returns empty).
    """
    generation = queries.graph_generation()
    if not queries.agent_exists(agent):
        return {
            "agent": agent,
            "agent_found": False,
            "count": 0,
            "tools": [],
            "graph_generation": generation,
        }
    tools = queries.unsafe_callable_tools(agent)
    if not include_authorized:
        tools = [t for t in tools if t.get("classification") == "violation"]
    return {
        "agent": agent,
        "agent_found": True,
        "count": len(tools),
        "tools": tools,
        "graph_generation": generation,
    }


@mcp.tool()
def blast_radius(node: str) -> dict:
    """Agents/tools affected if `node` (a resource URI or a policy id) changes."""
    result = queries.blast_radius(node)
    result["graph_generation"] = queries.graph_generation()
    return result


@mcp.tool()
def unmapped_tools(only_granted: bool = True) -> dict:
    """Granted tools (or all tools if `only_granted=False`) with no authored
    READS/WRITES — data-flow effects unclassified."""
    rows = queries.unmapped_tools(only_granted=only_granted)
    return {"count": len(rows), "tools": rows, "graph_generation": queries.graph_generation()}


@mcp.tool()
def orphan_policies() -> dict:
    """Policies no agent currently reaches — likely obsolete or over-narrow."""
    rows = queries.orphan_policies()
    return {"count": len(rows), "policies": rows, "graph_generation": queries.graph_generation()}


@mcp.tool()
def unbacked_edges(include_grants: bool = False) -> dict:
    """Authored edges with no usable evidence pointer.

    Default scope is READS/WRITES/GOVERNED_BY — load-bearing claims about the
    world. Pass ``include_grants=True`` to also surface CAN_CALL grants
    (intent declarations, usually noise unless your policy requires every
    grant to cite a change-request / runbook).
    """
    rows = queries.unbacked_edges(include_grants=include_grants)
    return {"count": len(rows), "edges": rows, "graph_generation": queries.graph_generation()}


@mcp.tool()
def drifted_tools() -> dict:
    """Tools with authored governance but no live EXPOSES edge — re-ingest needed."""
    rows = queries.drifted_tools()
    return {"count": len(rows), "tools": rows, "graph_generation": queries.graph_generation()}


if __name__ == "__main__":
    mcp.run()

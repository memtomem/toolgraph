"""toolgraph as an MCP server: agents query the registry over the same protocol
it crawls. Thin wrappers over graph.queries — all logic lives there.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from toolgraph.graph import queries

mcp = FastMCP("toolgraph")


@mcp.tool()
def check_access(agent: str, tool: str) -> dict:
    """Can `agent` call `tool`, and does the call path cross a DENY policy?

    Returns a verdict (ALLOW | DENY | NOT_GRANTED | TOOL_NOT_FOUND) with the
    exact resource/policy evidence path.
    """
    return queries.check_access(agent, tool)


@mcp.tool()
def unsafe_callable_tools(agent: str) -> dict:
    """Tools `agent` is allowed to call that reach a DENY policy.

    Returns an object (not a bare list) so the MCP structured-content shape is
    stable: ``{"agent", "count", "tools": [...]}``.
    """
    tools = queries.unsafe_callable_tools(agent)
    return {"agent": agent, "count": len(tools), "tools": tools}


@mcp.tool()
def blast_radius(node: str) -> dict:
    """Agents/tools affected if `node` (a resource URI or a policy id) changes."""
    return queries.blast_radius(node)


@mcp.tool()
def unmapped_tools(only_granted: bool = True) -> dict:
    """Granted tools (or all tools if `only_granted=False`) with no authored
    READS/WRITES — data-flow effects unclassified."""
    rows = queries.unmapped_tools(only_granted=only_granted)
    return {"count": len(rows), "tools": rows}


@mcp.tool()
def orphan_policies() -> dict:
    """Policies no agent currently reaches — likely obsolete or over-narrow."""
    rows = queries.orphan_policies()
    return {"count": len(rows), "policies": rows}


@mcp.tool()
def unbacked_edges() -> dict:
    """Authored edges with source='operator_asserted' and no evidence pointer."""
    rows = queries.unbacked_edges()
    return {"count": len(rows), "edges": rows}


@mcp.tool()
def drifted_tools() -> dict:
    """Tools with authored governance but no live EXPOSES edge — re-ingest needed."""
    rows = queries.drifted_tools()
    return {"count": len(rows), "tools": rows}


if __name__ == "__main__":
    mcp.run()

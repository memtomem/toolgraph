"""toolgraph as an MCP server: agents query the registry over the same protocol
it crawls. Thin wrappers over graph.queries — all logic lives there.

Every response carries ``graph_generation`` (ADR-0004) so callers can cache
graph-derived features and pin telemetry to a replayable graph state. The
stamp lives HERE, not in the query layer — CLI output stays unchanged.
"""

from __future__ import annotations

from collections.abc import Callable

from mcp.server.fastmcp import FastMCP

from toolgraph.graph import queries

mcp = FastMCP("toolgraph")

_GENERATION_RETRIES = 5


def _with_generation(fetch: Callable[[], dict]) -> dict:
    """Run ``fetch`` and stamp a generation that actually brackets it.

    Neo4j is read-committed, not snapshot-isolated: the result and the
    generation are separate reads, so a concurrent crawl/ingest could commit
    between them and mispair graph state with generation — exactly what
    ADR-0004's cache/replay consumers must never see. Bracket the fetch with
    two generation reads and retry while they disagree; equality proves no
    successful write committed in between (every write bumps the counter in
    its own transaction).
    """
    for _ in range(_GENERATION_RETRIES):
        before = queries.graph_generation()
        result = fetch()
        if queries.graph_generation() == before:
            result["graph_generation"] = before
            return result
    # Write churn outlasted every retry (a long crawl burst). Stamp the
    # freshest value: at worst the label trails the still-in-flight burst,
    # and generation-keyed caches self-heal on their next read.
    result["graph_generation"] = queries.graph_generation()
    return result


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
    return _with_generation(lambda: queries.check_access(agent, tool))


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

    def fetch() -> dict:
        if not queries.agent_exists(agent):
            return {"agent": agent, "agent_found": False, "count": 0, "tools": []}
        tools = queries.unsafe_callable_tools(agent)
        if not include_authorized:
            tools = [t for t in tools if t.get("classification") == "violation"]
        return {"agent": agent, "agent_found": True, "count": len(tools), "tools": tools}

    return _with_generation(fetch)


@mcp.tool()
def blast_radius(node: str) -> dict:
    """Agents/tools affected if `node` (a resource URI or a policy id) changes."""
    return _with_generation(lambda: queries.blast_radius(node))


@mcp.tool()
def unmapped_tools(only_granted: bool = True) -> dict:
    """Granted tools (or all tools if `only_granted=False`) with no authored
    READS/WRITES — data-flow effects unclassified."""

    def fetch() -> dict:
        rows = queries.unmapped_tools(only_granted=only_granted)
        return {"count": len(rows), "tools": rows}

    return _with_generation(fetch)


@mcp.tool()
def orphan_policies() -> dict:
    """Policies no agent currently reaches — likely obsolete or over-narrow."""

    def fetch() -> dict:
        rows = queries.orphan_policies()
        return {"count": len(rows), "policies": rows}

    return _with_generation(fetch)


@mcp.tool()
def unbacked_edges(include_grants: bool = False) -> dict:
    """Authored edges with no usable evidence pointer.

    Default scope is READS/WRITES/GOVERNED_BY — load-bearing claims about the
    world. Pass ``include_grants=True`` to also surface CAN_CALL grants
    (intent declarations, usually noise unless your policy requires every
    grant to cite a change-request / runbook).
    """

    def fetch() -> dict:
        rows = queries.unbacked_edges(include_grants=include_grants)
        return {"count": len(rows), "edges": rows}

    return _with_generation(fetch)


@mcp.tool()
def drifted_tools() -> dict:
    """Tools with authored governance but no live EXPOSES edge — re-ingest needed."""

    def fetch() -> dict:
        rows = queries.drifted_tools()
        return {"count": len(rows), "tools": rows}

    return _with_generation(fetch)


if __name__ == "__main__":
    mcp.run()

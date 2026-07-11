"""toolgraph as an MCP server: agents query the registry over the same protocol
it crawls. Thin wrappers over graph.queries — all logic lives there.

Every response carries ``graph_generation`` (ADR-0004) so callers can cache
graph-derived features and pin telemetry to a replayable graph state. The
stamp lives HERE, not in the query layer — CLI output stays unchanged.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from toolgraph.graph import queries, selector

mcp = FastMCP("toolgraph")

_with_generation = queries.with_generation


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


@mcp.tool()
def destructive_unsafeguarded() -> dict:
    """Tools hinting ``destructiveHint: true`` with no GOVERNED_BY path —
    neither directly on the tool nor via any resource it reads/writes.

    The hint is the server's self-claim (``crawled``/``medium``, ADR-0006):
    these rows are an authoring priority queue, not violations. Annotations
    never auto-create edges; the operator authors the policy binding.
    """

    def fetch() -> dict:
        rows = queries.destructive_unsafeguarded()
        return {"count": len(rows), "tools": rows}

    return _with_generation(fetch)


@mcp.tool()
def annotation_contradictions() -> dict:
    """Authored WRITES edges on tools hinting ``readOnlyHint: true``.

    One side is wrong — the server's self-claim or the operator's assertion.
    Each row cites both provenances so the operator can chase the stronger
    evidence; the analyzer never picks a winner (ADR-0006).
    """

    def fetch() -> dict:
        rows = queries.annotation_contradictions()
        return {"count": len(rows), "contradictions": rows}

    return _with_generation(fetch)


@mcp.tool()
def audit_report(include_grants: bool = False) -> dict:
    """Whole-graph operational audit summary.

    Aggregates unsafe DENY violations, operator-authorized DENY paths, drift,
    unmapped tools, unbacked evidence, orphan policies, destructive annotation
    gaps, and annotation contradictions. ``status`` is ``fail`` only for
    blocking findings (unsafe violations or drifted governed tools), ``warn``
    for advisory findings, and ``pass`` when the graph is clean.
    """
    return _with_generation(lambda: queries.audit_report(include_grants=include_grants))


@mcp.tool()
def rank_features(agent: str, candidates: list[str]) -> dict:
    """Batch selection features for every candidate, in input order (ADR-0005).

    Facts only — resolution (found/ambiguous/tool_key), grant, verdict, DENY
    classification + evidence paths, drift, mapping, evidence coverage, the
    four annotation self-claims — plus the rule-based ``risk_score`` from the
    published fixed table. No relevance, no learning. ``agent_found: false``
    means selection should abort: context-construction error, not an empty
    result. Cache per ``graph_generation``.
    """
    return _with_generation(lambda: selector.rank_features(agent, candidates))


@mcp.tool()
def eligible_tools(
    agent: str, candidates: list[str], profile: str = selector.DEFAULT_PROFILE
) -> dict:
    """Hard-filter candidates with reject reasons + policy-evidence paths.

    ``profile`` is a named rule set — strict (production default) / review /
    explore — not a learned threshold. ``eligible`` holds resolved tool keys
    in candidate input order; each rejected row carries the input ref, the
    winning reason, and for DENY reasons the graph paths that prove it. A
    learned consumer may rerank ``eligible`` but may never resurrect a
    rejected row (ADR-0005).
    """
    return _with_generation(
        lambda: selector.eligible_tools(agent, candidates, profile=profile)
    )


@mcp.tool()
def selection_explain(
    agent: str, tool: str, profile: str = selector.DEFAULT_PROFILE
) -> dict:
    """Compact human-readable reasons why `tool` is eligible/rejected for
    `agent` under `profile` — a view over the same facts ``rank_features``
    returns, for operators and end users."""
    return _with_generation(
        lambda: selector.selection_explain(agent, tool, profile=profile)
    )


if __name__ == "__main__":
    mcp.run()

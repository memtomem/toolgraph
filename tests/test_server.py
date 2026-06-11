"""Phase 5: end-to-end — connect an MCP client to the toolgraph server itself
and confirm every exposed tool round-trips its documented shape over the
protocol (not just check_access; the four negative-truth wrappers are
covered too to catch shape regressions in production).
"""

from __future__ import annotations

import json
import sys

import pytest

from toolgraph.crawler.transports import open_session
from toolgraph.graph import loader, queries
from toolgraph.manifest.ingest import ingest_governance
from toolgraph.models import (
    AccessGrant,
    CrawlResult,
    DataAccess,
    Governance,
    Policy,
    ResourceRecord,
    ServerSpec,
    ToolRecord,
)

CSV = "file:///data/customers.csv"


def _seed() -> None:
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file"), ToolRecord(name="ping")],
            resources=[ResourceRecord(uri=CSV)],
        )
    )
    ingest_governance(
        Governance(
            agents=["support-bot"],
            policies=[Policy(id="pii-deny", effect="DENY")],
            grants=[
                AccessGrant(agent="support-bot", tool="sample::read_file"),
                AccessGrant(agent="support-bot", tool="sample::ping"),  # unmapped
            ],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["pii-deny"]},
        )
    )


def _spec(neo4j_container) -> ServerSpec:
    return ServerSpec(
        name="toolgraph",
        transport="stdio",
        command=sys.executable,
        args=["-m", "toolgraph.server.app"],
        env={
            "NEO4J_URI": neo4j_container.get_connection_url(),
            "NEO4J_USER": "neo4j",
            "NEO4J_PASSWORD": "testpass",
        },
    )


def _structured(result) -> dict:
    got = result.structuredContent
    if got is None:  # fall back to text content
        got = json.loads(result.content[0].text)
    return got


async def test_mcp_tool_matches_direct_query(graph, neo4j_container):
    _seed()
    async with open_session(_spec(neo4j_container)) as session:
        await session.initialize()
        listed = await session.list_tools()
        assert {t.name for t in listed.tools} == {
            "check_access",
            "unsafe_callable_tools",
            "blast_radius",
            "unmapped_tools",
            "orphan_policies",
            "unbacked_edges",
            "drifted_tools",
        }

        result = await session.call_tool(
            "check_access", {"agent": "support-bot", "tool": "read_file"}
        )
        got = _structured(result)

    # The MCP wrapper stamps graph_generation on top of the query result
    # (ADR-0004); the query layer itself stays unstamped for the CLI.
    expected = queries.check_access("support-bot", "read_file")
    expected["graph_generation"] = queries.graph_generation()
    assert got == expected
    assert got["verdict"] == "DENY"


# Each (mcp_tool, args, collection_key) — the wrapper documented shape is
# {count: int, <collection_key>: list}. count must equal len(collection).
_WRAPPER_CASES = [
    ("unsafe_callable_tools", {"agent": "support-bot"}, "tools"),
    ("unmapped_tools", {}, "tools"),
    ("orphan_policies", {}, "policies"),
    ("unbacked_edges", {}, "edges"),
    ("drifted_tools", {}, "tools"),
]


@pytest.mark.parametrize("tool_name,args,collection_key", _WRAPPER_CASES)
async def test_mcp_wrapper_returns_documented_shape(
    graph, neo4j_container, tool_name, args, collection_key
):
    """Every wrapper in server/app.py returns {count, <collection>}; the four
    new negative-truth tools were only name-asserted before. A rename or
    list-return regression would now fail here, not in production."""
    _seed()
    async with open_session(_spec(neo4j_container)) as session:
        await session.initialize()
        result = await session.call_tool(tool_name, args)
        got = _structured(result)
    assert isinstance(got, dict), f"{tool_name} returned a bare list, not a dict"
    assert "count" in got, f"{tool_name} missing 'count' key"
    assert collection_key in got, f"{tool_name} missing {collection_key!r} key"
    assert isinstance(got[collection_key], list)
    assert got["count"] == len(got[collection_key])


async def test_mcp_blast_radius_carries_found_flag(graph, neo4j_container):
    """blast_radius must return found:False for a typo'd URI so MCP callers
    can distinguish 'no impact' from 'unknown node'."""
    _seed()
    async with open_session(_spec(neo4j_container)) as session:
        await session.initialize()
        got = _structured(await session.call_tool(
            "blast_radius", {"node": "file:///does/not/exist"}
        ))
    assert got["found"] is False
    assert got["impacted"] == []


async def test_mcp_unbacked_edges_respects_include_grants(graph, neo4j_container):
    """The include_grants param landed at the query AND wrapper layer — call
    it twice and confirm the wider scope returns ≥ the narrower."""
    _seed()
    async with open_session(_spec(neo4j_container)) as session:
        await session.initialize()
        default = _structured(await session.call_tool("unbacked_edges", {}))
        wider = _structured(await session.call_tool(
            "unbacked_edges", {"include_grants": True}
        ))
    assert wider["count"] >= default["count"]
    assert {e["edge_type"] for e in default["edges"]}.isdisjoint({"CAN_CALL"})

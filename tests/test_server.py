"""Phase 5: end-to-end — connect an MCP client to the toolgraph server itself
and confirm a tool call returns the same result as the direct Cypher query.
"""

from __future__ import annotations

import json
import sys

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
            tools=[ToolRecord(name="read_file")],
            resources=[ResourceRecord(uri=CSV)],
        )
    )
    ingest_governance(
        Governance(
            agents=["support-bot"],
            policies=[Policy(id="pii-deny", effect="DENY")],
            grants=[AccessGrant(agent="support-bot", tool="sample::read_file")],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["pii-deny"]},
        )
    )


async def test_mcp_tool_matches_direct_query(graph, neo4j_container):
    _seed()

    # Run the toolgraph MCP server as a subprocess, pointed at the same container.
    spec = ServerSpec(
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

    async with open_session(spec) as session:
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
        got = result.structuredContent
        if got is None:  # fall back to text content
            got = json.loads(result.content[0].text)

    expected = queries.check_access("support-bot", "read_file")
    assert got == expected
    assert got["verdict"] == "DENY"

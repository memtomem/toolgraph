"""Phase 5: end-to-end — connect an MCP client to the toolgraph server itself
and confirm every exposed tool round-trips its documented shape over the
protocol (not just check_access; the four negative-truth wrappers are
covered too to catch shape regressions in production).
"""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager

import pytest

from tests._helpers import CSV, seed_basic
from toolgraph import config
from toolgraph.crawler.transports import open_session
from toolgraph.graph import driver, loader, queries
from toolgraph.manifest.ingest import ingest_governance
from toolgraph.models import (
    AccessGrant,
    CrawlResult,
    DataAccess,
    ExpectedException,
    Governance,
    Policy,
    ResourceRecord,
    ServerSpec,
    ToolRecord,
)


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


@pytest.fixture
def server_spec(backend_graph, tmp_path) -> ServerSpec:
    """Launch the server on the test's backend; a missing config file hides any ambient one."""
    settings = config.settings
    env = {"TOOLGRAPH_CONFIG": str(tmp_path / "no-config.json")}
    if backend_graph == "neo4j":
        # No TOOLGRAPH_BACKEND: this keeps covering the NEO4J_*-only fallback
        # that ADR-0017 step 3 retires.
        env.update(
            NEO4J_URI=settings.neo4j_uri,
            NEO4J_USER=settings.neo4j_user,
            NEO4J_PASSWORD=settings.neo4j_password,
        )
    else:
        env.update(TOOLGRAPH_BACKEND="ladybug", TOOLGRAPH_DB_PATH=str(settings.db_path))
    return ServerSpec(
        name="toolgraph",
        transport="stdio",
        command=sys.executable,
        args=["-m", "toolgraph.server.app"],
        env=env,
    )


@asynccontextmanager
async def _connect(spec: ServerSpec):
    # Ladybug allows one owning process, so the test releases its handle
    # before the server opens the file; later direct queries reopen it.
    driver.close_driver()
    async with open_session(spec) as session:
        await session.initialize()
        yield session


def _structured(result) -> dict:
    got = result.structured_content
    if got is None:  # fall back to text content
        got = json.loads(result.content[0].text)
    return got


async def test_mcp_tool_matches_direct_query(server_spec):
    _seed()
    async with _connect(server_spec) as session:
        listed = await session.list_tools()
        assert {t.name for t in listed.tools} == {
            "check_access",
            "unsafe_callable_tools",
            "blast_radius",
            "unmapped_tools",
            "orphan_policies",
            "unbacked_edges",
            "drifted_tools",
            "destructive_unsafeguarded",
            "annotation_contradictions",
            "audit_report",
            "rank_features",
            "eligible_tools",
            "selection_explain",
        }

        result = await session.call_tool(
            "check_access", {"agent": "support-bot", "tool": "read_file"}
        )
        got = _structured(result)

    # The MCP wrapper stamps graph_generation on top of the query result
    # (ADR-0004); the query layer itself stays unstamped for the CLI.
    expected = queries.check_access("support-bot", "read_file")
    state = queries.graph_state()
    expected.update(
        graph_generation=state.generation,
        graph_instance_id=state.instance_id,
        graph_state={"instance_id": state.instance_id, "generation": state.generation},
        graph_state_verified=True,
    )
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
    ("destructive_unsafeguarded", {}, "tools"),
    ("annotation_contradictions", {}, "contradictions"),
]


@pytest.mark.parametrize("tool_name,args,collection_key", _WRAPPER_CASES)
async def test_mcp_wrapper_returns_documented_shape(server_spec, tool_name, args, collection_key):
    """Every wrapper in server/app.py returns {count, <collection>}; the four
    new negative-truth tools were only name-asserted before. A rename or
    list-return regression would now fail here, not in production."""
    _seed()
    async with _connect(server_spec) as session:
        result = await session.call_tool(tool_name, args)
        got = _structured(result)
    assert isinstance(got, dict), f"{tool_name} returned a bare list, not a dict"
    assert "count" in got, f"{tool_name} missing 'count' key"
    assert collection_key in got, f"{tool_name} missing {collection_key!r} key"
    assert isinstance(got[collection_key], list)
    assert got["count"] == len(got[collection_key])


async def test_mcp_selector_surface_mirrors_direct_query(server_spec):
    """ADR-0005 gate criterion: the MCP wrappers return the same structured
    shape as the direct selector functions, plus the generation stamp."""
    from toolgraph.graph import selector

    _seed()
    candidates = ["read_file", "sample::ping", "nope"]
    async with _connect(server_spec) as session:
        ranked = _structured(
            await session.call_tool(
                "rank_features", {"agent": "support-bot", "candidates": candidates}
            )
        )
        filtered = _structured(
            await session.call_tool(
                "eligible_tools",
                {"agent": "support-bot", "candidates": candidates, "profile": "review"},
            )
        )
        explained = _structured(
            await session.call_tool(
                "selection_explain", {"agent": "support-bot", "tool": "read_file"}
            )
        )

    state = queries.graph_state()
    for got, expected in (
        (ranked, selector.rank_features("support-bot", candidates)),
        (filtered, selector.eligible_tools("support-bot", candidates, profile="review")),
        (explained, selector.selection_explain("support-bot", "read_file")),
    ):
        expected.update(
            graph_generation=state.generation,
            graph_instance_id=state.instance_id,
            graph_state={"instance_id": state.instance_id, "generation": state.generation},
            graph_state_verified=True,
        )
        assert got == expected


async def test_mcp_blast_radius_carries_found_flag(server_spec):
    """blast_radius must return found:False for a typo'd URI so MCP callers
    can distinguish 'no impact' from 'unknown node'."""
    _seed()
    async with _connect(server_spec) as session:
        got = _structured(
            await session.call_tool("blast_radius", {"node": "file:///does/not/exist"})
        )
    assert got["found"] is False
    assert got["impacted"] == []


async def test_mcp_unbacked_edges_respects_include_grants(server_spec):
    """The include_grants param landed at the query AND wrapper layer — call
    it twice and confirm the wider scope returns ≥ the narrower."""
    _seed()
    async with _connect(server_spec) as session:
        default = _structured(await session.call_tool("unbacked_edges", {}))
        wider = _structured(await session.call_tool("unbacked_edges", {"include_grants": True}))
    assert wider["count"] >= default["count"]
    assert {e["edge_type"] for e in default["edges"]}.isdisjoint({"CAN_CALL"})


async def test_mcp_audit_report_mirrors_direct_query(server_spec):
    _seed()
    async with _connect(server_spec) as session:
        got = _structured(await session.call_tool("audit_report", {}))

    expected = queries.audit_report()
    state = queries.graph_state()
    expected.update(
        graph_generation=state.generation,
        graph_instance_id=state.instance_id,
        graph_state={"instance_id": state.instance_id, "generation": state.generation},
        graph_state_verified=True,
    )
    assert got == expected


def test_mcp_unsafe_callable_tools_wrapper_filters_by_default(backend_graph):
    """The MCP wrapper must apply the same violation-only default as the CLI
    — agents querying over MCP shouldn't see authorized_but_governed
    rows mixed in."""
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file"), ToolRecord(name="write_file")],
            resources=[ResourceRecord(uri=CSV)],
        )
    )
    ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="pii-deny", effect="DENY")],
            grants=[
                AccessGrant(agent="planner", tool="sample::read_file"),
                AccessGrant(agent="planner", tool="sample::write_file"),
            ],
            data_access=[
                DataAccess(tool="sample::read_file", resource=CSV, mode="READS"),
                DataAccess(tool="sample::write_file", resource=CSV, mode="WRITES"),
            ],
            governed_by={CSV: ["pii-deny"]},
            expected_exceptions=[
                ExpectedException(
                    agent="planner",
                    tool="sample::read_file",
                    policy="pii-deny",
                    resource=CSV,
                    reason="by design",
                )
            ],
        )
    )
    # Direct invocation of the @mcp.tool() function (no MCP transport needed
    # for this assertion — we're testing the wrapper, not the protocol).
    from toolgraph.server.app import unsafe_callable_tools as wrap

    default = wrap.fn("planner") if hasattr(wrap, "fn") else wrap("planner")
    assert default["agent_found"] is True
    assert default["count"] == 1
    assert default["tools"][0]["tool"] == "write_file"
    assert all(t["classification"] == "violation" for t in default["tools"])

    full = (
        wrap.fn("planner", include_authorized=True)
        if hasattr(wrap, "fn")
        else wrap("planner", include_authorized=True)
    )
    assert full["count"] == 2
    classifications = {t["classification"] for t in full["tools"]}
    assert classifications == {"violation", "authorized_but_governed"}


def test_mcp_unsafe_callable_tools_wrapper_signals_unknown_agent(backend_graph):
    seed_basic(backend_graph)
    from toolgraph.server.app import unsafe_callable_tools as wrap

    res = wrap.fn("planer") if hasattr(wrap, "fn") else wrap("planer")
    assert res["agent_found"] is False
    assert res["count"] == 0
    assert res["tools"] == []

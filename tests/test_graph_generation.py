"""ADR-0004: graph_generation — a monotonic id stamped at crawl/ingest.

The invariant under test: generation moves if and only if the graph does.
Every successful crawl load and manifest ingest bumps it in the same
transaction; a rejected ingest (atomicity contract, ADR-0002) leaves both
the graph AND the generation untouched, so caches keyed on it stay valid.
"""

from __future__ import annotations

import pytest

from toolgraph.graph import loader, queries
from toolgraph.manifest.ingest import ingest_governance
from toolgraph.models import (
    AccessGrant,
    CrawlResult,
    DataAccess,
    Governance,
    Policy,
    ResourceRecord,
    ToolRecord,
)

CSV = "file:///data/customers.csv"


def _crawl() -> CrawlResult:
    return CrawlResult(
        server_name="sample",
        transport="stdio",
        tools=[ToolRecord(name="read_file"), ToolRecord(name="ping")],
        resources=[ResourceRecord(uri=CSV)],
    )


def _governance() -> Governance:
    return Governance(
        agents=["support-bot"],
        policies=[Policy(id="pii-deny", effect="DENY")],
        grants=[AccessGrant(agent="support-bot", tool="sample::read_file")],
        data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
        governed_by={CSV: ["pii-deny"]},
    )


def test_generation_is_zero_on_empty_graph(graph):
    assert queries.graph_generation() == 0


def test_crawl_load_increments_even_when_idempotent(graph):
    loader.load_crawl_result(_crawl())
    assert queries.graph_generation() == 1
    # An identical re-crawl is a no-op on nodes/edges but still a successful
    # load — the token tracks "graph may have changed", not "graph differs".
    loader.load_crawl_result(_crawl())
    assert queries.graph_generation() == 2


def test_clean_ingest_increments_rejected_ingest_does_not(graph):
    loader.load_crawl_result(_crawl())
    before = queries.graph_generation()

    report = ingest_governance(_governance())
    assert report.applied
    assert queries.graph_generation() == before + 1

    after_clean = queries.graph_generation()
    rejected = ingest_governance(
        Governance(
            agents=["support-bot"],
            grants=[AccessGrant(agent="support-bot", tool="sample::no_such_tool")],
        )
    )
    assert not rejected.applied
    # Rejection returns before Phase 2 — graph untouched, generation untouched.
    assert queries.graph_generation() == after_clean


def test_mcp_responses_carry_graph_generation(graph):
    loader.load_crawl_result(_crawl())
    ingest_governance(_governance())
    # Direct invocation of the @mcp.tool() functions (no MCP transport needed
    # for this assertion — we're testing the wrappers, not the protocol).
    from toolgraph.server.app import check_access, unmapped_tools

    expected = queries.graph_generation()
    assert expected == 2  # one crawl + one ingest

    got = (
        check_access.fn("support-bot", "read_file")
        if hasattr(check_access, "fn")
        else check_access("support-bot", "read_file")
    )
    assert got["graph_generation"] == expected

    got = unmapped_tools.fn() if hasattr(unmapped_tools, "fn") else unmapped_tools()
    assert got["graph_generation"] == expected


def test_mcp_stamp_retries_when_generation_moves_mid_read(graph, monkeypatch):
    """Codex review of PR #22: result and generation are separate reads under
    read-committed isolation, so a write landing between them must not mispair
    state with generation. The bracket (read gen, fetch, read gen, compare)
    retries until stable: simulate a crawl committing mid-fetch."""
    from toolgraph.server import app
    from toolgraph.server.app import check_access

    loader.load_crawl_result(_crawl())
    ingest_governance(_governance())

    # Generation sequence across bracket reads: 2, then 3 (a write slipped
    # in after the first fetch), then stable 3s — first bracket fails,
    # second succeeds and must stamp 3.
    seq = iter([2, 3, 3, 3])
    monkeypatch.setattr(
        app.queries,
        "graph_state",
        lambda: queries.GraphState("test-instance", next(seq)),
    )
    fetches = 0
    real_check_access = app.queries.check_access

    def counting_check_access(agent, tool):
        nonlocal fetches
        fetches += 1
        return real_check_access(agent, tool)

    monkeypatch.setattr(app.queries, "check_access", counting_check_access)

    fn = check_access.fn if hasattr(check_access, "fn") else check_access
    got = fn("support-bot", "read_file")
    assert got["graph_generation"] == 3
    assert got["graph_instance_id"] == "test-instance"
    assert got["graph_state"] == {"instance_id": "test-instance", "generation": 3}
    assert fetches == 2  # first bracket discarded, second accepted


def test_generation_bracket_fails_instead_of_mislabelling_after_retry_exhaustion(monkeypatch):
    seq = iter(range(20))
    monkeypatch.setattr(queries, "graph_generation", lambda: next(seq))
    with pytest.raises(RuntimeError, match="generation changed"):
        queries.with_generation(lambda: {"value": "from-moving-graph"}, strict=True)


def test_generation_bracket_live_query_self_heals_after_retry_exhaustion(monkeypatch):
    seq = iter(range(20))
    monkeypatch.setattr(queries, "graph_generation", lambda: next(seq))
    assert queries.with_generation(lambda: {"value": "live"}) == {
        "value": "live", "graph_generation": 10,
    }

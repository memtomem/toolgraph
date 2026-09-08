"""Phase 3: authored-governance ingest — idempotency, edges, unresolved refs."""

from __future__ import annotations

from toolgraph.graph import driver, loader
from toolgraph.manifest.ingest import governance_counts, ingest_governance
from toolgraph.models import (
    AccessGrant,
    CrawlResult,
    DataAccess,
    Governance,
    Policy,
    ResourceRecord,
    ToolRecord,
)


def _seed_crawl() -> None:
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file"), ToolRecord(name="write_file")],
            resources=[ResourceRecord(uri="file:///data/customers.csv")],
        )
    )


def _governance() -> Governance:
    return Governance(
        agents=["planner", "support-bot"],
        policies=[Policy(id="pii-deny", effect="DENY", scope="customer-pii")],
        grants=[
            AccessGrant(agent="planner", tool="sample::read_file"),
            AccessGrant(agent="planner", tool="sample::write_file"),
            AccessGrant(agent="support-bot", tool="sample::read_file"),
        ],
        data_access=[
            DataAccess(tool="sample::read_file", resource="file:///data/customers.csv", mode="READS"),
            DataAccess(tool="sample::write_file", resource="file:///data/customers.csv", mode="WRITES"),
        ],
        governed_by={"file:///data/customers.csv": ["pii-deny"]},
    )


def test_ingest_is_idempotent(graph):
    _seed_crawl()
    gov = _governance()

    assert ingest_governance(gov).warnings == []  # no unresolved refs
    first = governance_counts()
    assert ingest_governance(gov).warnings == []
    second = governance_counts()

    assert first == second
    assert first == {
        "agent": 2,
        "policy": 1,
        "can_call": 3,
        "reads": 1,
        "writes": 1,
        "governed_by": 1,
        "expected_exception": 0,
    }


def test_unresolved_tool_ref_warns(graph):
    _seed_crawl()
    gov = Governance(
        agents=["planner"],
        grants=[AccessGrant(agent="planner", tool="sample::does_not_exist")],
    )
    warnings = ingest_governance(gov).warnings
    assert any("does_not_exist" in w for w in warnings)


def test_duplicate_grant_entries_keep_the_later_manifest_entry(graph):
    """UNWIND batching must preserve the old per-row last-write-wins rule."""
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file")],
        )
    )
    report = ingest_governance(
        Governance(
            agents=["planner"],
            grants=[
                AccessGrant(agent="planner", tool="sample::read_file", granted_by="first"),
                AccessGrant(agent="planner", tool="sample::read_file", granted_by="second"),
            ],
        )
    )
    assert report.warnings == []
    with driver.session() as s:
        row = s.run(
            "MATCH (:Agent {id:'planner'})-[r:CAN_CALL]->(:Tool {key:'sample::read_file'}) "
            "RETURN r.granted_by AS granted_by"
        ).single()
    assert row["granted_by"] == "second"

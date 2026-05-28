"""Classification of unsafe-tools reach: violation vs authorized_but_governed.

Codex review of the gate exhibit caught that `unsafe_callable_tools(planner)`
labeled planner's by-design grants as 'unsafe' — wrong framing for an
advisory analyzer. Operators now declare intentional DENY-zone reach in the
manifest's ``expected_exceptions`` block; matching rows surface as
``authorized_but_governed`` with the operator's stated reason, leaving real
unauthorized reach as ``violation`` so reviewers don't drown in noise.
"""

from __future__ import annotations

from toolgraph.graph import loader, queries
from toolgraph.manifest.ingest import ingest_governance
from toolgraph.models import (
    AccessGrant,
    CrawlResult,
    DataAccess,
    ExpectedException,
    Governance,
    Policy,
    ResourceRecord,
    ToolRecord,
)

CSV = "file:///data/customers.csv"


def _seed(graph) -> None:
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file"), ToolRecord(name="write_file")],
            resources=[ResourceRecord(uri=CSV)],
        )
    )


def _gov(*exceptions: ExpectedException) -> Governance:
    return Governance(
        agents=["planner", "support-bot"],
        policies=[Policy(id="pii-deny", effect="DENY", scope="customer-pii")],
        grants=[
            AccessGrant(agent="planner", tool="sample::read_file"),
            AccessGrant(agent="planner", tool="sample::write_file"),
            AccessGrant(agent="support-bot", tool="sample::read_file"),
        ],
        data_access=[
            DataAccess(tool="sample::read_file", resource=CSV, mode="READS"),
            DataAccess(tool="sample::write_file", resource=CSV, mode="WRITES"),
        ],
        governed_by={CSV: ["pii-deny"]},
        expected_exceptions=list(exceptions),
    )


def test_no_exceptions_means_every_reach_is_violation(graph):
    _seed(graph)
    assert ingest_governance(_gov()) == []
    rows = queries.unsafe_callable_tools("planner")
    assert {(r["tool"], r["classification"]) for r in rows} == {
        ("read_file", "violation"),
        ("write_file", "violation"),
    }


def test_matching_exception_marks_row_authorized_but_governed(graph):
    _seed(graph)
    assert ingest_governance(
        _gov(
            ExpectedException(
                agent="planner",
                tool="sample::read_file",
                policy="pii-deny",
                resource=CSV,
                reason="planner is the PII data owner; reads are by design",
            )
        )
    ) == []
    by_tool = {r["tool"]: r for r in queries.unsafe_callable_tools("planner")}
    assert by_tool["read_file"]["classification"] == "authorized_but_governed"
    assert by_tool["read_file"]["exception_reason"].startswith("planner is the PII")
    # write_file has no exception — stays a violation
    assert by_tool["write_file"]["classification"] == "violation"
    assert "exception_reason" not in by_tool["write_file"]


def test_exception_with_no_resource_covers_every_resource(graph):
    _seed(graph)
    assert ingest_governance(
        _gov(
            ExpectedException(
                agent="planner",
                tool="sample::write_file",
                policy="pii-deny",
                # resource omitted -> matches any resource under this policy
                reason="planner is on-call; emergency writes are authorized",
            )
        )
    ) == []
    by_tool = {r["tool"]: r for r in queries.unsafe_callable_tools("planner")}
    assert by_tool["write_file"]["classification"] == "authorized_but_governed"


def test_check_access_evidence_carries_classification_when_agent_known(graph):
    _seed(graph)
    assert ingest_governance(
        _gov(
            ExpectedException(
                agent="planner",
                tool="sample::read_file",
                policy="pii-deny",
                resource=CSV,
                reason="by design",
            )
        )
    ) == []
    res = queries.check_access("planner", "read_file")
    assert res["verdict"] == "DENY"  # verdict semantics unchanged
    [ev] = res["deny_evidence"]
    assert ev["classification"] == "authorized_but_governed"
    assert ev["exception_reason"] == "by design"

    # support-bot has no exception declared -> evidence is a plain violation
    res = queries.check_access("support-bot", "read_file")
    [ev] = res["deny_evidence"]
    assert ev["classification"] == "violation"


def test_unknown_policy_or_tool_in_exception_rejects_manifest(graph):
    _seed(graph)
    warnings = ingest_governance(
        _gov(
            ExpectedException(
                agent="planner", tool="sample::nope", policy="pii-deny", reason="x"
            )
        )
    )
    assert any("nope" in w for w in warnings)
    warnings = ingest_governance(
        _gov(
            ExpectedException(
                agent="planner", tool="sample::read_file", policy="no-such-policy", reason="x"
            )
        )
    )
    assert any("no-such-policy" in w for w in warnings)

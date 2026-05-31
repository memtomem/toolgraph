"""Phase 4: core governance queries, against a hand-traced scenario.

Scenario:
  planner     CAN_CALL read_file, write_file
  support-bot CAN_CALL read_file
  read_file   READS  customers.csv   (DENY: pii-deny)
  write_file  WRITES customers.csv   (DENY: pii-deny)
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


@pytest.fixture
def scenario(graph):
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file"), ToolRecord(name="write_file")],
            resources=[ResourceRecord(uri=CSV)],
        )
    )
    gov = Governance(
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
    )
    assert ingest_governance(gov) == []


def test_check_access_deny(scenario):
    res = queries.check_access("support-bot", "read_file")
    assert res["verdict"] == "DENY"
    assert res["permitted"] is True
    assert res["deny_evidence"][0]["resource"] == CSV
    assert res["deny_evidence"][0]["policy"] == "pii-deny"
    assert "GOVERNED_BY" in res["deny_evidence"][0]["path"]


def test_check_access_not_granted(scenario):
    res = queries.check_access("support-bot", "write_file")
    assert res["verdict"] == "NOT_GRANTED"
    assert res["permitted"] is False


def test_check_access_tool_not_found(scenario):
    res = queries.check_access("planner", "nope")
    assert res["found"] is False
    assert res["verdict"] == "TOOL_NOT_FOUND"


def test_unsafe_callable_tools(scenario):
    rows = queries.unsafe_callable_tools("planner")
    tools = sorted(r["tool"] for r in rows)
    assert tools == ["read_file", "write_file"]
    assert all(r["policy"] == "pii-deny" for r in rows)
    # support-bot can only call read_file -> exactly one unsafe tool
    assert [r["tool"] for r in queries.unsafe_callable_tools("support-bot")] == ["read_file"]


def test_blast_radius_resource(scenario):
    res = queries.blast_radius(CSV)
    assert res["kind"] == "resource"
    affected = {(r["agent"], r["tool"], r["mode"]) for r in res["impacted"]}
    assert affected == {
        ("planner", "read_file", "READS"),
        ("planner", "write_file", "WRITES"),
        ("support-bot", "read_file", "READS"),
    }


def test_blast_radius_policy(scenario):
    res = queries.blast_radius("pii-deny")
    assert res["kind"] == "policy"
    assert all(r["resource"] == CSV for r in res["impacted"])
    agents = {r["agent"] for r in res["impacted"]}
    assert agents == {"planner", "support-bot"}

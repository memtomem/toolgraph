"""Phase 4: core governance queries, against a hand-traced scenario.

Scenario:
  planner     CAN_CALL read_file, write_file
  support-bot CAN_CALL read_file
  read_file   READS  customers.csv   (DENY: pii-deny)
  write_file  WRITES customers.csv   (DENY: pii-deny)
"""

from __future__ import annotations

import pytest

from tests._helpers import CSV, load_crawl, seed_basic
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
    assert ingest_governance(gov).warnings == []


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


def test_blast_radius_policy_omits_governed_resource_with_no_tool(graph):
    """A policy can govern a resource before any tool READS/WRITES it. That
    should keep found=True for the policy, but it is not an impacted tool row."""
    ingest_governance(
        Governance(
            policies=[Policy(id="pii-deny", effect="DENY", scope="customer-pii")],
            governed_by={CSV: ["pii-deny"]},
        )
    )

    res = queries.blast_radius("pii-deny")
    assert res == {
        "kind": "policy",
        "node": "pii-deny",
        "found": True,
        "impacted": [],
    }


def test_check_access_returns_agent_not_found_for_unknown_agent(graph):
    """Typo'd agent must NOT collapse into NOT_GRANTED — that hides bugs.
    An operator iterating on names needs the distinct verdict to tell
    'this agent has no grant on this tool' from 'this agent doesn't exist'."""
    seed_basic(graph)
    res = queries.check_access("planer", "read_file")  # typo
    assert res["verdict"] == "AGENT_NOT_FOUND"
    assert res["agent_found"] is False

    # known agent stays unaffected (verdict NOT_GRANTED / DENY / ALLOW)
    res = queries.check_access("planner", "read_file")
    assert res["verdict"] == "DENY"


def test_blast_radius_resource_marks_found_false_for_typo(graph):
    seed_basic(graph)
    res = queries.blast_radius("file:///does/not/exist")
    assert res["kind"] == "resource"
    assert res["found"] is False
    assert res["impacted"] == []

    # real resource: found=True
    res = queries.blast_radius(CSV)
    assert res["found"] is True
    assert res["impacted"], "should report planner's reach to CSV"


def test_blast_radius_policy_marks_found_false_for_typo(graph):
    seed_basic(graph)
    res = queries.blast_radius("no-such-policy")
    assert res["kind"] == "policy"
    assert res["found"] is False
    assert res["impacted"] == []

    res = queries.blast_radius("pii-deny")
    assert res["found"] is True


def test_check_access_agent_found_is_set_on_every_branch(graph):
    """Callers using ``result['agent_found']`` shouldn't KeyError on the
    success branches just because the field was originally added only for
    the failure case."""
    seed_basic(graph)
    # DENY branch
    res = queries.check_access("planner", "read_file")
    assert res["verdict"] == "DENY"
    assert res["agent_found"] is True

    # NOT_GRANTED branch
    ingest_governance(
        Governance(
            agents=["loner"],  # known agent, no grants
            policies=[Policy(id="pii-deny", effect="DENY")],
            grants=[],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["pii-deny"]},
        )
    )
    res = queries.check_access("loner", "read_file")
    assert res["verdict"] == "NOT_GRANTED"
    assert res["agent_found"] is True

    # TOOL_NOT_FOUND branch (still need a real agent)
    res = queries.check_access("loner", "no_such_tool")
    assert res["verdict"] == "TOOL_NOT_FOUND"
    assert res["agent_found"] is True

    # AGENT_NOT_FOUND
    res = queries.check_access("planer", "read_file")
    assert res["verdict"] == "AGENT_NOT_FOUND"
    assert res["agent_found"] is False


def test_blast_radius_falls_back_to_policy_for_uri_shaped_policy_id(graph):
    """is_resource_ref is purely lexical: a policy id like 'urn:secret' looks
    URI-shaped. If no Resource matches the URI, blast_radius now retries the
    same string as a Policy id before declaring not-found."""
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="leak")],
            resources=[],
        )
    )
    ingest_governance(
        Governance(
            agents=["bot"],
            policies=[Policy(id="urn:secret", effect="DENY")],
            grants=[AccessGrant(agent="bot", tool="sample::leak")],
            data_access=[DataAccess(tool="sample::leak", resource="file:///x", mode="WRITES")],
            governed_by={"sample::leak": ["urn:secret"]},
        )
    )
    res = queries.blast_radius("urn:secret")
    # Re-routed from resource-branch (no match) to policy-branch.
    assert res["kind"] == "policy"
    assert res["found"] is True
    assert any(r["via"] == "tool" for r in res["impacted"])


def test_check_access_agent_not_found_carries_found_none(graph):
    """README promises every response carries agent_found AND found. For
    AGENT_NOT_FOUND the tool lookup didn't run, so `found` is None (not
    False — hard-coding False would lie about a tool that actually exists).
    Three-state contract; callers should use result.get('found')."""
    seed_basic(graph)
    res = queries.check_access("planer", "read_file")
    assert res["verdict"] == "AGENT_NOT_FOUND"
    assert res["agent_found"] is False
    assert res["found"] is None


def test_ambiguous_bare_name_in_query(graph):
    load_crawl("alpha", ["read_file"])
    load_crawl("beta", ["read_file"])
    # AGENT_NOT_FOUND is checked before tool resolution (outer concern), so we
    # need a real Agent for AMBIGUOUS_TOOL to actually surface.
    ingest_governance(Governance(agents=["planner"]))
    res = queries.check_access("planner", "read_file")
    assert res["verdict"] == "AMBIGUOUS_TOOL"
    assert sorted(res["candidates"]) == ["alpha::read_file", "beta::read_file"]


def test_tool_level_deny_is_honored(graph):
    load_crawl("sample", ["dangerous_tool"])
    ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="block", effect="DENY")],
            grants=[AccessGrant(agent="planner", tool="sample::dangerous_tool")],
            governed_by={"sample::dangerous_tool": ["block"]},
        )
    )
    res = queries.check_access("planner", "dangerous_tool")
    assert res["verdict"] == "DENY"
    assert res["deny_evidence"][0]["via"] == "tool"

    unsafe = queries.unsafe_callable_tools("planner")
    assert [u["tool"] for u in unsafe] == ["dangerous_tool"]
    assert unsafe[0]["via"] == "tool"


def test_blast_radius_includes_tool_without_caller(graph):
    load_crawl("sample", ["leak"])
    ingest_governance(
        Governance(
            data_access=[DataAccess(tool="sample::leak", resource="file:///secret", mode="WRITES")]
        )
    )
    rows = queries.blast_radius("file:///secret")["impacted"]
    assert any(r["tool"] == "leak" and r["agent"] is None for r in rows)


def test_blast_radius_policy_rows_have_path_key(graph):
    load_crawl("sample", ["leak"])
    ingest_governance(
        Governance(
            policies=[Policy(id="deny", effect="DENY")],
            data_access=[DataAccess(tool="sample::leak", resource="file:///secret", mode="WRITES")],
            governed_by={"file:///secret": ["deny"]},
        )
    )
    impacted = queries.blast_radius("deny")["impacted"]
    assert impacted and all("path" in r for r in impacted)


def test_policy_blast_includes_ungranted_tool(graph):
    load_crawl("sample", ["leak"])
    ingest_governance(
        Governance(
            policies=[Policy(id="deny", effect="DENY")],
            data_access=[DataAccess(tool="sample::leak", resource="file:///secret", mode="WRITES")],
            governed_by={"file:///secret": ["deny"]},
        )
    )
    impacted = queries.blast_radius("deny")["impacted"]
    assert any(r["tool"] == "leak" and r["agent"] is None for r in impacted)

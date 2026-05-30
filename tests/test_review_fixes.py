"""Regression tests for the issues the Codex review surfaced."""

from __future__ import annotations

from toolgraph.crawler.crawl import crawl_all
from toolgraph.graph import loader, queries
from toolgraph.manifest.ingest import governance_counts, ingest_governance
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
from toolgraph.uris import is_resource_ref

CSV = "file:///data/customers.csv"


def _load(server: str, tool_names: list[str], resources: list[str] | None = None) -> None:
    loader.load_crawl_result(
        CrawlResult(
            server_name=server,
            transport="stdio",
            tools=[ToolRecord(name=n) for n in tool_names],
            resources=[ResourceRecord(uri=u) for u in (resources or [])],
        )
    )


# --- BLOCKER: ambiguous bare tool names ----------------------------------


def test_ambiguous_bare_name_in_query(graph):
    _load("alpha", ["read_file"])
    _load("beta", ["read_file"])
    # AGENT_NOT_FOUND is checked before tool resolution (outer concern), so we
    # need a real Agent for AMBIGUOUS_TOOL to actually surface.
    ingest_governance(Governance(agents=["planner"]))
    res = queries.check_access("planner", "read_file")
    assert res["verdict"] == "AMBIGUOUS_TOOL"
    assert sorted(res["candidates"]) == ["alpha::read_file", "beta::read_file"]


def test_ambiguous_bare_name_in_ingest_warns(graph):
    _load("alpha", ["read_file"])
    _load("beta", ["read_file"])
    warnings = ingest_governance(
        Governance(agents=["planner"], grants=[AccessGrant(agent="planner", tool="read_file")])
    )
    assert any("ambiguous" in w for w in warnings)
    # exact key still resolves
    assert ingest_governance(
        Governance(agents=["planner"], grants=[AccessGrant(agent="planner", tool="alpha::read_file")])
    ) == []


# --- HIGH: tool-level GOVERNED_BY honored --------------------------------


def test_tool_level_deny_is_honored(graph):
    _load("sample", ["dangerous_tool"])
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


# --- HIGH: missing policy id warns ---------------------------------------


def test_missing_policy_id_warns(graph):
    _load("sample", ["read_file"])
    warnings = ingest_governance(
        Governance(
            policies=[Policy(id="real", effect="DENY")],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["typo-policy"]},
        )
    )
    assert any("typo-policy" in w and "not defined" in w for w in warnings)
    # the bogus deny edge must not exist -> access is not falsely DENY'd
    assert governance_counts()["governed_by"] == 0


# --- HIGH: declarative manifest (revocation) -----------------------------


def test_revoking_a_grant_removes_the_edge(graph):
    _load("sample", ["read_file"])
    full = Governance(
        agents=["planner"],
        grants=[AccessGrant(agent="planner", tool="sample::read_file")],
    )
    ingest_governance(full)
    assert governance_counts()["can_call"] == 1

    # re-ingest without the grant -> it is removed
    ingest_governance(Governance(agents=["planner"]))
    assert governance_counts()["can_call"] == 0


# --- HIGH: crawl reconciliation (stale tool removal) ---------------------


def test_recrawl_removes_stale_tool(graph):
    _load("sample", ["read_file", "write_file"])
    assert loader.graph_counts()["tool"] == 2
    _load("sample", ["read_file"])  # write_file gone
    counts = loader.graph_counts()
    assert counts["tool"] == 1
    assert counts["exposes"] == 1


# --- MEDIUM: per-server crawl timeout isolation --------------------------


def test_crawl_timeout_is_isolated():
    hang = ServerSpec(name="hang", transport="stdio", command="sleep", args=["30"])
    results, failures = crawl_all([hang], timeout=1.0)
    assert results == []
    assert failures and "timeout" in failures[0][1]


# --- code-review round 2 fixes -------------------------------------------


def test_resource_uri_normalized_across_crawl_and_manifest(graph):
    # Crawl stores the normalized URI ('https://svc.example.com/'); the manifest
    # is authored without the trailing slash. They must resolve to one node.
    loader.load_crawl_result(
        CrawlResult(
            server_name="api",
            transport="stdio",
            tools=[ToolRecord(name="call")],
            resources=[ResourceRecord(uri="https://svc.example.com")],
        )
    )
    ingest_governance(
        Governance(
            agents=["bot"],
            policies=[Policy(id="deny", effect="DENY")],
            grants=[AccessGrant(agent="bot", tool="api::call")],
            data_access=[DataAccess(tool="api::call", resource="https://svc.example.com", mode="READS")],
            governed_by={"https://svc.example.com": ["deny"]},
        )
    )
    assert queries.check_access("bot", "call")["verdict"] == "DENY"


def test_recrawl_keeps_governed_tool_and_warns(graph):
    _load("sample", ["read_file", "write_file"])
    ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="deny", effect="DENY")],
            grants=[AccessGrant(agent="planner", tool="sample::write_file")],
            governed_by={"sample::write_file": ["deny"]},
        )
    )
    warnings = loader.load_crawl_result(
        CrawlResult(server_name="sample", transport="stdio", tools=[ToolRecord(name="read_file")])
    )
    assert any("write_file" in w for w in warnings)
    # governance survived the recrawl rather than being silently dropped
    res = queries.check_access("planner", "sample::write_file")
    assert res["found"] is True
    assert res["verdict"] == "DENY"


def test_blast_radius_includes_tool_without_caller(graph):
    _load("sample", ["leak"])
    ingest_governance(
        Governance(
            data_access=[DataAccess(tool="sample::leak", resource="file:///secret", mode="WRITES")]
        )
    )
    rows = queries.blast_radius("file:///secret")["impacted"]
    assert any(r["tool"] == "leak" and r["agent"] is None for r in rows)


def test_blast_radius_policy_rows_have_path_key(graph):
    _load("sample", ["leak"])
    ingest_governance(
        Governance(
            policies=[Policy(id="deny", effect="DENY")],
            data_access=[DataAccess(tool="sample::leak", resource="file:///secret", mode="WRITES")],
            governed_by={"file:///secret": ["deny"]},
        )
    )
    impacted = queries.blast_radius("deny")["impacted"]
    assert impacted and all("path" in r for r in impacted)


# --- code-review round 3 (PR) fixes --------------------------------------


def test_failed_ingest_does_not_mutate_graph(graph):
    _load("sample", ["read_file"])
    good = Governance(
        agents=["planner"],
        policies=[Policy(id="deny", effect="DENY")],
        grants=[AccessGrant(agent="planner", tool="sample::read_file")],
        data_access=[DataAccess(tool="sample::read_file", resource="file:///x", mode="READS")],
        governed_by={"file:///x": ["deny"]},
    )
    assert ingest_governance(good) == []
    before = governance_counts()
    assert queries.check_access("planner", "read_file")["verdict"] == "DENY"

    # a manifest with a typo'd policy must be rejected whole — not partially applied
    bad = good.model_copy(update={"governed_by": {"file:///x": ["dney"]}})
    warnings = ingest_governance(bad)
    assert warnings  # rejected
    assert governance_counts() == before  # graph untouched
    assert queries.check_access("planner", "read_file")["verdict"] == "DENY"  # DENY preserved


def test_policy_blast_includes_ungranted_tool(graph):
    _load("sample", ["leak"])
    ingest_governance(
        Governance(
            policies=[Policy(id="deny", effect="DENY")],
            data_access=[DataAccess(tool="sample::leak", resource="file:///secret", mode="WRITES")],
            governed_by={"file:///secret": ["deny"]},
        )
    )
    impacted = queries.blast_radius("deny")["impacted"]
    assert any(r["tool"] == "leak" and r["agent"] is None for r in impacted)


def test_non_hierarchical_uri_treated_as_resource(graph):
    assert is_resource_ref("memory:secret")
    assert is_resource_ref("urn:x")
    assert is_resource_ref("file:/tmp/a")
    assert not is_resource_ref("sample::read_file")
    assert not is_resource_ref("read_file")

    _load("sample", ["read_file"])
    ingest_governance(
        Governance(
            agents=["bot"],
            policies=[Policy(id="deny", effect="DENY")],
            grants=[AccessGrant(agent="bot", tool="sample::read_file")],
            data_access=[DataAccess(tool="sample::read_file", resource="memory:secret", mode="READS")],
            governed_by={"memory:secret": ["deny"]},
        )
    )
    assert queries.check_access("bot", "read_file")["verdict"] == "DENY"

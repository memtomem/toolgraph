"""Negative-truth / drift queries: surface what the graph DOESN'T say.

Codex review of the gate exhibit noted that an advisory analyzer answering
only positive-reachability questions can look more authoritative than it is.
These tests cover the audit views that make unspecified, unsupported, and
stale parts of the graph visible.
"""

from __future__ import annotations

from toolgraph.graph import driver, loader, queries
from toolgraph.manifest.ingest import ingest_governance
from toolgraph.models import (
    AccessGrant,
    CrawlResult,
    DataAccess,
    ExpectedException,
    Governance,
    GovernedByBinding,
    Policy,
    Provenance,
    ResourceRecord,
    ToolRecord,
)


def _seed_three_tools(graph) -> None:
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[
                ToolRecord(name="read_file"),
                ToolRecord(name="write_file"),
                ToolRecord(name="ping"),  # untouched, no grant, no mapping
            ],
            resources=[ResourceRecord(uri="file:///data/customers.csv")],
        )
    )


# --- unmapped_tools -------------------------------------------------------


def test_unmapped_tools_reports_granted_tools_with_no_data_access(graph):
    _seed_three_tools(graph)
    ingest_governance(
        Governance(
            agents=["planner"],
            grants=[
                AccessGrant(agent="planner", tool="sample::read_file"),
                AccessGrant(agent="planner", tool="sample::write_file"),
            ],
            data_access=[
                DataAccess(
                    tool="sample::read_file",
                    resource="file:///data/customers.csv",
                    mode="READS",
                ),
            ],  # write_file granted but not classified
        )
    )
    rows = queries.unmapped_tools()
    assert len(rows) == 1
    assert rows[0]["tool_key"] == "sample::write_file"
    assert rows[0]["granted_to"] == ["planner"]


def test_unmapped_tools_all_includes_crawled_but_ungranted_tools(graph):
    _seed_three_tools(graph)
    # no governance at all
    all_unmapped = {r["tool_key"] for r in queries.unmapped_tools(only_granted=False)}
    assert all_unmapped == {"sample::read_file", "sample::write_file", "sample::ping"}
    # default (only_granted=True) is empty because nothing has CAN_CALL
    assert queries.unmapped_tools() == []


# --- orphan_policies ------------------------------------------------------


def test_orphan_policy_flagged_when_no_reachable_agent(graph):
    _seed_three_tools(graph)
    ingest_governance(
        Governance(
            policies=[
                Policy(id="reachable", effect="DENY", scope="data"),
                Policy(id="orphan", effect="DENY", scope="nothing"),
            ],
            agents=["planner"],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[
                DataAccess(
                    tool="sample::read_file",
                    resource="file:///data/customers.csv",
                    mode="READS",
                ),
            ],
            governed_by={"file:///data/customers.csv": ["reachable"]},
        )
    )
    orphans = {r["policy"] for r in queries.orphan_policies()}
    assert orphans == {"orphan"}


# --- unbacked_edges -------------------------------------------------------


def test_unbacked_edges_default_excludes_can_call_grants(graph):
    """CAN_CALL is intent, not a factual claim — evidence shouldn't be required
    by default. Otherwise every grant pollutes the audit output."""
    _seed_three_tools(graph)
    ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="pii", effect="DENY", scope="pii")],
            grants=[
                AccessGrant(agent="planner", tool="sample::read_file"),
                AccessGrant(
                    agent="planner",
                    tool="sample::write_file",
                    provenance=Provenance(evidence="ops/grants/Q2.md#L8"),
                ),
            ],
            data_access=[
                DataAccess(
                    tool="sample::read_file",
                    resource="file:///data/customers.csv",
                    mode="READS",
                ),
            ],
            governed_by={"file:///data/customers.csv": ["pii"]},
        )
    )
    rows = queries.unbacked_edges()
    edge_signatures = {(r["edge_type"], r["src"], r["dst"]) for r in rows}
    # CAN_CALL excluded by default — even the grant with no evidence
    assert ("CAN_CALL", "planner", "sample::read_file") not in edge_signatures
    assert ("CAN_CALL", "planner", "sample::write_file") not in edge_signatures
    # READS / GOVERNED_BY ARE surfaced (factual claims)
    assert ("READS", "sample::read_file", "file:///data/customers.csv") in edge_signatures
    assert ("GOVERNED_BY", "file:///data/customers.csv", "pii") in edge_signatures


def test_unbacked_edges_include_grants_surfaces_can_call(graph):
    """With ``include_grants=True``, the unbacked CAN_CALL grant appears and
    the one with evidence does not."""
    _seed_three_tools(graph)
    ingest_governance(
        Governance(
            agents=["planner"],
            grants=[
                AccessGrant(agent="planner", tool="sample::read_file"),
                AccessGrant(
                    agent="planner",
                    tool="sample::write_file",
                    provenance=Provenance(evidence="ops/grants/Q2.md#L8"),
                ),
            ],
        )
    )
    rows = queries.unbacked_edges(include_grants=True)
    edge_signatures = {(r["edge_type"], r["src"], r["dst"]) for r in rows}
    assert ("CAN_CALL", "planner", "sample::read_file") in edge_signatures
    assert ("CAN_CALL", "planner", "sample::write_file") not in edge_signatures


def test_unbacked_edges_governed_by_binding_evidence_suppresses_row(graph):
    """A GOVERNED_BY edge written via the binding-object form with evidence
    must not appear in unbacked_edges; the bare-string form still does."""
    _seed_three_tools(graph)
    ingest_governance(
        Governance(
            agents=["planner"],
            policies=[
                Policy(id="pii", effect="DENY", scope="pii"),
                Policy(id="other", effect="DENY", scope="other"),
            ],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[
                DataAccess(
                    tool="sample::read_file",
                    resource="file:///data/customers.csv",
                    mode="READS",
                    provenance=Provenance(evidence="da.md"),
                ),
            ],
            governed_by={
                "file:///data/customers.csv": [
                    # binding with evidence -> NOT unbacked
                    GovernedByBinding(
                        policy="pii",
                        provenance=Provenance(evidence="ops/policy/pii.md"),
                    ),
                    # bare string -> still unbacked
                    "other",
                ]
            },
        )
    )
    rows = queries.unbacked_edges()
    governed_rows = [r for r in rows if r["edge_type"] == "GOVERNED_BY"]
    assert {(r["src"], r["dst"]) for r in governed_rows} == {
        ("file:///data/customers.csv", "other"),
    }


def test_unbacked_edges_treats_blank_evidence_as_missing(graph):
    """Empty or whitespace evidence is not a usable source pointer. It should
    be audited the same way as omitted evidence."""
    _seed_three_tools(graph)
    ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="pii", effect="DENY", scope="pii")],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[
                DataAccess(
                    tool="sample::read_file",
                    resource="file:///data/customers.csv",
                    mode="READS",
                    provenance=Provenance(evidence="   "),
                ),
            ],
            governed_by={
                "file:///data/customers.csv": [
                    GovernedByBinding(
                        policy="pii",
                        provenance=Provenance(evidence=""),
                    ),
                ]
            },
        )
    )

    rows = queries.unbacked_edges()
    edge_signatures = {(r["edge_type"], r["src"], r["dst"]) for r in rows}
    assert ("READS", "sample::read_file", "file:///data/customers.csv") in edge_signatures
    assert ("GOVERNED_BY", "file:///data/customers.csv", "pii") in edge_signatures


def test_unbacked_edges_includes_inferred_claims_without_evidence(graph):
    """An inferred data-flow edge is still unsupported without a citation to
    the inference source. It should not be hidden just because source is not
    operator_asserted."""
    _seed_three_tools(graph)
    ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="pii", effect="DENY", scope="pii")],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[
                DataAccess(
                    tool="sample::read_file",
                    resource="file:///data/customers.csv",
                    mode="READS",
                    provenance=Provenance(source="inferred", confidence="medium"),
                ),
            ],
            governed_by={
                "file:///data/customers.csv": [
                    GovernedByBinding(
                        policy="pii",
                        provenance=Provenance(evidence="ops/policy/pii.md"),
                    ),
                ]
            },
        )
    )

    rows = queries.unbacked_edges()
    read_rows = [
        r
        for r in rows
        if r["edge_type"] == "READS"
        and r["src"] == "sample::read_file"
        and r["dst"] == "file:///data/customers.csv"
    ]
    assert read_rows == [
        {
            "edge_type": "READS",
            "src_label": "Tool",
            "src": "sample::read_file",
            "dst_label": "Resource",
            "dst": "file:///data/customers.csv",
            "source": "inferred",
            "confidence": "medium",
        }
    ]


# --- drifted_tools --------------------------------------------------------


def test_drift_surfaces_tools_with_governance_but_no_exposes(graph):
    _seed_three_tools(graph)
    ingest_governance(
        Governance(
            agents=["planner"],
            grants=[AccessGrant(agent="planner", tool="sample::write_file")],
            data_access=[
                DataAccess(
                    tool="sample::write_file",
                    resource="file:///data/customers.csv",
                    mode="WRITES",
                ),
            ],
        )
    )
    # Simulate a re-crawl where write_file disappeared from the server (and
    # ping too, but ping has no governance so it gets DETACH DELETEd; only
    # write_file is kept because it carries authored edges).
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file")],  # write_file + ping gone
            resources=[ResourceRecord(uri="file:///data/customers.csv")],
        )
    )
    rows = queries.drifted_tools()
    assert len(rows) == 1
    assert rows[0]["tool_key"] == "sample::write_file"
    assert rows[0]["granted_to"] == ["planner"]
    # Sanity: live tool with no drift not listed.
    with driver.session() as s:
        assert s.run("MATCH (t:Tool {key:'sample::read_file'}) RETURN t").single() is not None


def test_drift_data_access_is_empty_when_only_grant_exists(graph):
    """A drifted tool can be governance-relevant because an agent can call it,
    even before data_access is authored. The audit row should report an empty
    list, not a placeholder map full of null values."""
    _seed_three_tools(graph)
    ingest_governance(
        Governance(
            agents=["planner"],
            grants=[AccessGrant(agent="planner", tool="sample::write_file")],
        )
    )
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file")],
            resources=[ResourceRecord(uri="file:///data/customers.csv")],
        )
    )

    rows = queries.drifted_tools()
    assert rows == [
        {
            "tool_key": "sample::write_file",
            "tool": "write_file",
            "server": "sample",
            "granted_to": ["planner"],
            "data_access": [],
        }
    ]


# --- audit_report ---------------------------------------------------------


def test_audit_report_aggregates_whole_graph_findings(graph):
    csv = "file:///data/customers.csv"
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[
                ToolRecord(name="read_file"),
                ToolRecord(name="write_file"),
                ToolRecord(name="delete_file", destructive_hint=True),
                ToolRecord(name="status", read_only_hint=True),
            ],
            resources=[ResourceRecord(uri=csv)],
        )
    )
    ingest_governance(
        Governance(
            agents=["planner", "reviewer"],
            policies=[
                Policy(id="pii", effect="DENY"),
                Policy(id="orphan", effect="DENY"),
            ],
            grants=[
                AccessGrant(agent="planner", tool="sample::read_file"),
                AccessGrant(agent="reviewer", tool="sample::read_file"),
                AccessGrant(agent="planner", tool="sample::write_file"),
                AccessGrant(agent="planner", tool="sample::delete_file"),
            ],
            data_access=[
                DataAccess(tool="sample::read_file", resource=csv, mode="READS"),
                DataAccess(
                    tool="sample::status",
                    resource=csv,
                    mode="WRITES",
                    provenance=Provenance(evidence="tests/status-writes.md:1"),
                ),
            ],
            governed_by={csv: ["pii"]},
            expected_exceptions=[
                ExpectedException(
                    agent="reviewer",
                    tool="sample::read_file",
                    policy="pii",
                    resource=csv,
                    reason="break-glass review",
                )
            ],
        )
    )
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[
                ToolRecord(name="read_file"),
                ToolRecord(name="delete_file", destructive_hint=True),
                ToolRecord(name="status", read_only_hint=True),
            ],
            resources=[ResourceRecord(uri=csv)],
        )
    )

    report = queries.audit_report()

    assert report["status"] == "fail"
    assert report["summary"]["agents"] == 2
    assert report["summary"]["blocking_findings"] == [
        "unsafe_violations",
        "drifted_tools",
    ]
    assert report["summary"]["counts"] == {
        "unsafe_violations": 1,
        "authorized_but_governed": 1,
        "drifted_tools": 1,
        "unmapped_tools": 2,
        "unbacked_edges": 2,
        "orphan_policies": 1,
        "destructive_unsafeguarded": 1,
        "annotation_contradictions": 1,
    }
    assert report["findings"]["unsafe_violations"][0]["agent"] == "planner"
    assert report["findings"]["authorized_but_governed"][0]["agent"] == "reviewer"

    with_grants = queries.audit_report(include_grants=True)
    assert with_grants["summary"]["include_grants"] is True
    assert with_grants["summary"]["counts"]["unbacked_edges"] > 2


def test_audit_report_passes_empty_graph(graph):
    report = queries.audit_report()
    assert report["status"] == "pass"
    assert report["summary"]["blocking_findings"] == []
    assert all(count == 0 for count in report["summary"]["counts"].values())


def test_audit_report_warns_on_advisory_findings_only(graph):
    _seed_three_tools(graph)
    ingest_governance(
        Governance(
            agents=["planner"],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[
                DataAccess(
                    tool="sample::read_file",
                    resource="file:///data/customers.csv",
                    mode="READS",
                )
            ],
        )
    )

    report = queries.audit_report()

    assert report["status"] == "warn"
    assert report["summary"]["blocking_findings"] == []
    assert report["summary"]["counts"]["unbacked_edges"] == 1


def _seed_multi_agent_deny_graph() -> None:
    """Three agents over one resource-path DENY and one direct-tool DENY.

    beta holds the only exceptions — one resource-specific, one wildcard — so
    any cross-agent leak in the batched audit query shows up as a
    misclassified row for alpha or gamma.
    """
    csv = "file:///data/customers.csv"
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file"), ToolRecord(name="drop_table")],
            resources=[ResourceRecord(uri=csv)],
        )
    )
    ingest_governance(
        Governance(
            agents=["alpha", "beta", "gamma"],
            policies=[
                Policy(id="pii", effect="DENY"),
                Policy(id="schema-freeze", effect="DENY"),
            ],
            grants=[
                AccessGrant(agent=agent, tool=tool)
                for agent in ("alpha", "beta", "gamma")
                for tool in ("sample::read_file", "sample::drop_table")
            ],
            data_access=[
                DataAccess(tool="sample::read_file", resource=csv, mode="READS")
            ],
            # csv reached transitively (via='resource'); drop_table bound
            # directly (via='tool') so both branches of the batched query run.
            governed_by={csv: ["pii"], "sample::drop_table": ["schema-freeze"]},
            expected_exceptions=[
                ExpectedException(
                    agent="beta",
                    tool="sample::read_file",
                    policy="pii",
                    resource=csv,
                    reason="resource-scoped break-glass",
                ),
                ExpectedException(
                    agent="beta",
                    tool="sample::drop_table",
                    policy="schema-freeze",
                    reason="wildcard migration window",
                ),
            ],
        )
    )


def test_audit_report_matches_the_per_agent_composition_exactly(graph):
    """The batched audit pass must equal looping unsafe_callable_tools().

    audit_report stopped calling unsafe_callable_tools once per agent and now
    runs one batched query regrouped agent-major. Row content AND order are
    part of the output contract, so pin the equivalence rather than counts.
    """
    _seed_multi_agent_deny_graph()

    report = queries.audit_report()
    batched = (
        report["findings"]["unsafe_violations"]
        + report["findings"]["authorized_but_governed"]
    )

    per_agent = [
        {"agent": agent, **row}
        for agent in ("alpha", "beta", "gamma")
        for row in queries.unsafe_callable_tools(agent)
    ]

    # Same rows, and the batched regrouping preserves agent-major order.
    assert sorted(batched, key=lambda r: (r["agent"], r["path"])) == sorted(
        per_agent, key=lambda r: (r["agent"], r["path"])
    )
    assert [r["agent"] for r in report["findings"]["unsafe_violations"]] == sorted(
        r["agent"] for r in report["findings"]["unsafe_violations"]
    )


def test_batched_audit_never_leaks_one_agents_exception_to_another(graph):
    """Only beta declared exceptions; alpha and gamma must stay violations.

    The rewrite moved the agent predicate from an ExpectedException property
    map into a WHERE clause over the batched match — the failure mode is an
    exception matching every agent instead of its own.
    """
    _seed_multi_agent_deny_graph()

    report = queries.audit_report()
    classified = {
        (row["agent"], row["path"]): row
        for row in report["findings"]["unsafe_violations"]
        + report["findings"]["authorized_but_governed"]
    }

    # Both deny branches, for all three agents.
    assert len(classified) == 6
    for agent in ("alpha", "gamma"):
        rows = [r for (a, _), r in classified.items() if a == agent]
        assert [r["classification"] for r in rows] == ["violation", "violation"]
        assert all("exception_reason" not in r for r in rows)

    beta = {r["via"]: r for (a, _), r in classified.items() if a == "beta"}
    assert beta["resource"]["classification"] == "authorized_but_governed"
    assert beta["resource"]["exception_reason"] == "resource-scoped break-glass"
    assert beta["tool"]["classification"] == "authorized_but_governed"
    assert beta["tool"]["exception_reason"] == "wildcard migration window"

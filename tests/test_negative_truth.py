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

"""P1 truth-signal fixes (issues #8, #10):

- Phase 2 prunes authored-only Resource nodes the manifest dropped (ADR-0001),
  so blast_radius's ``found`` flag stays honest for resources — matching the
  Agent/Policy prune symmetry.
- Wildcard + resource-specific ExpectedException matching the same deny path
  yields ONE evidence row, carrying the specific exception's reason.
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
ENV = "file:///srv/app/.env"


def _crawl(graph):
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file")],
            resources=[ResourceRecord(uri=CSV)],
        )
    )


# --- #8: authored-only Resource nodes are pruned on re-ingest ------------


def test_phase2_prunes_authored_only_resource_on_re_ingest(graph):
    """ENV is authored-only (never crawled). Dropping it from the manifest
    must delete the node — otherwise blast_radius answers found:true /
    impacted:[] for a resource nothing governs or touches anymore."""
    _crawl(graph)
    base = dict(
        agents=["planner"],
        policies=[Policy(id="secrets-deny", effect="DENY")],
        grants=[AccessGrant(agent="planner", tool="sample::read_file")],
    )
    assert ingest_governance(
        Governance(
            **base,
            data_access=[DataAccess(tool="sample::read_file", resource=ENV, mode="READS")],
            governed_by={ENV: ["secrets-deny"]},
        )
    ).warnings == []
    assert queries.blast_radius(ENV)["found"] is True

    # Re-ingest without any mention of ENV.
    assert ingest_governance(Governance(**base)).warnings == []
    res = queries.blast_radius(ENV)
    assert res["found"] is False
    assert res["impacted"] == []


def test_phase2_keeps_crawled_resource_without_governance(graph):
    """A crawled resource (live PROVIDES) is the loader's property: ingest
    must never prune it, even when no manifest mentions it."""
    _crawl(graph)
    assert ingest_governance(Governance(agents=["planner"])).warnings == []
    assert queries.blast_radius(CSV)["found"] is True


# --- #10: wildcard + specific exception => one row, specific reason ------


def _governance_with_both_exceptions() -> Governance:
    return Governance(
        agents=["planner"],
        policies=[Policy(id="pii-deny", effect="DENY")],
        grants=[AccessGrant(agent="planner", tool="sample::read_file")],
        data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
        governed_by={CSV: ["pii-deny"]},
        expected_exceptions=[
            ExpectedException(
                agent="planner", tool="sample::read_file", policy="pii-deny",
                reason="wildcard: legacy blanket sign-off",
            ),
            ExpectedException(
                agent="planner", tool="sample::read_file", policy="pii-deny",
                resource=CSV, reason="specific: DPA ticket COMP-42",
            ),
        ],
    )


def test_unsafe_tools_one_row_when_wildcard_and_specific_both_match(graph):
    _crawl(graph)
    assert ingest_governance(_governance_with_both_exceptions()).warnings == []

    rows = queries.unsafe_callable_tools("planner")
    assert len(rows) == 1
    row = rows[0]
    assert row["classification"] == "authorized_but_governed"
    # The resource-specific exception outranks the wildcard for the reason.
    assert row["exception_reason"] == "specific: DPA ticket COMP-42"


def test_check_access_one_deny_row_when_wildcard_and_specific_both_match(graph):
    _crawl(graph)
    assert ingest_governance(_governance_with_both_exceptions()).warnings == []

    res = queries.check_access("planner", "read_file")
    assert res["verdict"] == "DENY"
    assert len(res["deny_evidence"]) == 1
    assert res["deny_evidence"][0]["exception_reason"] == "specific: DPA ticket COMP-42"
    assert res["all_authorized"] is True

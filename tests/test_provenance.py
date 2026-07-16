"""Per-edge provenance: source/confidence/evidence on every edge.

The graph must let a reader tell crawled facts from operator assertions, and
find supporting evidence for each assertion. EXPOSES/PROVIDES carry
``source='crawled'`` automatically; CAN_CALL/READS/WRITES default to
``operator_asserted/high`` and accept explicit provenance per edge.
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

CSV = "file:///data/customers.csv"


def _seed(graph) -> None:
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            endpoint="python tests/fixtures/sample_server.py",
            tools=[ToolRecord(name="read_file"), ToolRecord(name="write_file")],
            resources=[ResourceRecord(uri=CSV)],
        )
    )


def test_crawled_edges_carry_source_crawled(graph):
    _seed(graph)
    with driver.session() as s:
        rec = s.run(
            "MATCH ()-[e:EXPOSES]->(:Tool {name:'read_file'}) "
            "RETURN e.source AS source, e.confidence AS conf, e.evidence AS ev"
        ).single()
    assert rec["source"] == "crawled"
    assert rec["conf"] == "high"
    assert rec["ev"] == "crawled from stdio:python"


def test_authored_grant_defaults_to_operator_asserted(graph):
    _seed(graph)
    ingest_governance(
        Governance(
            agents=["planner"],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
        )
    )
    with driver.session() as s:
        rec = s.run(
            "MATCH (:Agent {id:'planner'})-[r:CAN_CALL]->() "
            "RETURN r.source AS source, r.confidence AS conf"
        ).single()
    assert rec["source"] == "operator_asserted"
    assert rec["conf"] == "high"


def test_explicit_provenance_roundtrips_to_graph(graph):
    _seed(graph)
    ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="pii-deny", effect="DENY", scope="customer-pii")],
            grants=[
                AccessGrant(
                    agent="planner",
                    tool="sample::read_file",
                    provenance=Provenance(evidence="grants.yaml#L17"),
                )
            ],
            data_access=[
                DataAccess(
                    tool="sample::read_file",
                    resource=CSV,
                    mode="READS",
                    provenance=Provenance(
                        source="inferred",
                        confidence="medium",
                        evidence="static-analysis: sample.py:42",
                    ),
                )
            ],
            # Per-binding provenance: cite WHY this resource is under this policy.
            # Replaces the dead Policy.provenance flattening of the previous round.
            governed_by={
                CSV: [
                    GovernedByBinding(
                        policy="pii-deny",
                        provenance=Provenance(evidence="ops/policy/pii-2026Q2.md"),
                    )
                ]
            },
        )
    )
    with driver.session() as s:
        grant = s.run(
            "MATCH ()-[r:CAN_CALL]->() RETURN r.evidence AS ev"
        ).single()
        access = s.run(
            "MATCH ()-[r:READS]->() "
            "RETURN r.source AS source, r.confidence AS conf, r.evidence AS ev"
        ).single()
        gov_edge = s.run(
            "MATCH (:Resource)-[r:GOVERNED_BY]->(:Policy {id:'pii-deny'}) "
            "RETURN r.source AS source, r.confidence AS conf, r.evidence AS ev"
        ).single()
    assert grant["ev"] == "grants.yaml#L17"
    assert access["source"] == "inferred"
    assert access["conf"] == "medium"
    assert "static-analysis" in access["ev"]
    # GOVERNED_BY edge now carries the per-binding evidence.
    assert gov_edge["source"] == "operator_asserted"
    assert gov_edge["conf"] == "high"
    assert gov_edge["ev"] == "ops/policy/pii-2026Q2.md"


def test_bare_string_governed_by_still_works(graph):
    """Backward compat: ``governed_by: {uri: [policy_id_str]}`` still parses,
    lifts to GovernedByBinding(policy=str, provenance=None), and writes a
    GOVERNED_BY edge with default source/confidence and null evidence.
    """
    _seed(graph)
    assert ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="pii-deny", effect="DENY", scope="customer-pii")],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[
                DataAccess(tool="sample::read_file", resource=CSV, mode="READS")
            ],
            governed_by={CSV: ["pii-deny"]},  # bare string, no provenance
        )
    ).warnings == []
    with driver.session() as s:
        gov_edge = s.run(
            "MATCH (:Resource)-[r:GOVERNED_BY]->(:Policy {id:'pii-deny'}) "
            "RETURN r.source AS source, r.evidence AS ev"
        ).single()
    assert gov_edge["source"] == "operator_asserted"
    assert gov_edge["ev"] is None


def test_policy_provenance_surfaces_on_deny_evidence_when_authored(graph):
    """When a GOVERNED_BY binding carries evidence, deny_evidence rows include
    a ``policy_provenance`` sub-dict. Default operator_asserted/no-evidence
    bindings DO NOT add the key (kept clean to avoid noise)."""
    _seed(graph)
    ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="pii-deny", effect="DENY", scope="customer-pii")],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[
                DataAccess(
                    tool="sample::read_file",
                    resource=CSV,
                    mode="READS",
                    provenance=Provenance(evidence="manifest.yaml#L42"),
                )
            ],
            governed_by={
                CSV: [
                    GovernedByBinding(
                        policy="pii-deny",
                        provenance=Provenance(evidence="ops/policy/pii.md#binding"),
                    )
                ]
            },
        )
    )
    res = queries.check_access("planner", "read_file")
    [ev] = res["deny_evidence"]
    assert ev["provenance"]["evidence"] == "manifest.yaml#L42"
    assert ev["policy_provenance"]["evidence"] == "ops/policy/pii.md#binding"

    # And with a bare-string binding, policy_provenance is NOT attached
    ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="pii-deny", effect="DENY", scope="customer-pii")],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[
                DataAccess(
                    tool="sample::read_file",
                    resource=CSV,
                    mode="READS",
                    provenance=Provenance(evidence="manifest.yaml#L42"),
                )
            ],
            governed_by={CSV: ["pii-deny"]},
        )
    )
    res = queries.check_access("planner", "read_file")
    [ev] = res["deny_evidence"]
    assert "policy_provenance" not in ev


def test_query_results_include_provenance(graph):
    _seed(graph)
    ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="pii-deny", effect="DENY", scope="customer-pii")],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[
                DataAccess(
                    tool="sample::read_file",
                    resource=CSV,
                    mode="READS",
                    provenance=Provenance(evidence="manifest.yaml#L42"),
                )
            ],
            governed_by={CSV: ["pii-deny"]},
        )
    )

    # check_access
    res = queries.check_access("planner", "read_file")
    assert res["verdict"] == "DENY"
    [ev] = res["deny_evidence"]
    assert ev["provenance"] == {
        "source": "operator_asserted",
        "confidence": "high",
        "evidence": "manifest.yaml#L42",
    }

    # unsafe_callable_tools
    [row] = queries.unsafe_callable_tools("planner")
    assert row["provenance"]["evidence"] == "manifest.yaml#L42"

    # blast_radius(resource) — every impacted row carries the READS provenance
    blast = queries.blast_radius(CSV)
    assert blast["impacted"], "should report planner's reach"
    for row in blast["impacted"]:
        assert row["provenance"]["evidence"] == "manifest.yaml#L42"

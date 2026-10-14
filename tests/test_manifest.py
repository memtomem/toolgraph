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
import pytest
import yaml
from pydantic import ValidationError
from toolgraph.graph import queries
from toolgraph.manifest.parser import load_governance
from toolgraph.manifest import ingest
from tests._helpers import CSV, crawl_sample, load_crawl, seed_basic


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


ENV = "file:///srv/app/.env"


class CountingSession:
    def __init__(self, session):
        self.session = session
        self.calls = []

    def run(self, query, **params):
        self.calls.append(params)
        return self.session.run(query, **params)


def test_phase2_prunes_orphan_agent_node_on_re_ingest(graph):
    """A manifest that drops an agent re-ingest must DELETE the old node so
    AGENT_NOT_FOUND fires for it (instead of returning silent []).
    Without pruning, R2's existence signals lie."""
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file")],
            resources=[ResourceRecord(uri=CSV)],
        )
    )
    # Ingest 1: planner + support-bot
    ingest_governance(
        Governance(
            agents=["planner", "support-bot"],
            policies=[Policy(id="pii-deny", effect="DENY")],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["pii-deny"]},
        )
    )
    assert queries.agent_exists("support-bot") is True

    # Ingest 2: only planner declared; support-bot dropped
    ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="pii-deny", effect="DENY")],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["pii-deny"]},
        )
    )
    assert queries.agent_exists("support-bot") is False
    # And check_access correctly reports AGENT_NOT_FOUND for the dropped agent
    res = queries.check_access("support-bot", "read_file")
    assert res["verdict"] == "AGENT_NOT_FOUND"


def test_phase2_prunes_orphan_policy_node_on_re_ingest(graph):
    """A policy dropped from the manifest must DELETE the old node so
    blast_radius(old-policy) returns found:False, not the misleading
    found:True with an empty impacted list."""
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file")],
            resources=[ResourceRecord(uri=CSV)],
        )
    )
    ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="old-deny", effect="DENY"), Policy(id="new-deny", effect="DENY")],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["new-deny"]},
        )
    )
    assert queries.blast_radius("old-deny")["found"] is True  # node exists, no impact

    # Drop old-deny — re-ingest with only new-deny
    ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="new-deny", effect="DENY")],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["new-deny"]},
        )
    )
    assert queries.blast_radius("old-deny")["found"] is False


def test_phase2_cleans_stale_prov_properties_on_policy_node(graph):
    """Pre-completion-round policies persisted prov_source/prov_confidence/
    prov_evidence on the node. The deletion path is REMOVE-on-MERGE so an
    upgrading graph doesn't leave stale node properties around."""
    # Simulate an old graph state: a Policy node with the pre-R2 prov_* shape.
    with driver.session() as s:
        s.run(
            "MERGE (p:Policy {id:'pii-deny'}) "
            "SET p.effect='DENY', p.prov_source='operator_asserted', "
            "p.prov_confidence='high', p.prov_evidence='legacy-evidence'"
        )

    # Ingest a current-shape manifest that names the same policy.
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file")],
            resources=[ResourceRecord(uri=CSV)],
        )
    )
    ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="pii-deny", effect="DENY")],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["pii-deny"]},
        )
    )
    with driver.session() as s:
        rec = s.run(
            "MATCH (p:Policy {id:'pii-deny'}) "
            "RETURN p.prov_source AS src, p.prov_confidence AS conf, p.prov_evidence AS ev"
        ).single()
    assert rec["src"] is None
    assert rec["conf"] is None
    assert rec["ev"] is None


def test_duplicate_policy_id_with_conflicting_effects_is_rejected(graph):
    """Codex R3.5: a manifest with two Policy entries sharing an id (e.g.
    [DENY, ALLOW] for 'legacy') silently overwrites effect on the Policy
    node via MERGE+SET. If the surviving effect is ALLOW, exceptions
    against 'legacy' pass deny-policy validation but then can never
    classify a row. Reject at ingest."""
    seed_basic(graph)
    warnings = ingest_governance(
        Governance(
            agents=["planner"],
            policies=[
                Policy(id="legacy", effect="DENY"),
                Policy(id="legacy", effect="ALLOW"),  # duplicate id
            ],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["legacy"]},
        )
    ).warnings
    assert warnings
    assert any("duplicate id" in w and "legacy" in w for w in warnings)


def test_policy_extra_forbid_rejects_legacy_provenance_block():
    """Old manifests carry policies[].provenance — Pydantic's default
    extra='ignore' silently dropped it. extra='forbid' makes the migration
    obvious with a clear error."""
    with pytest.raises(ValidationError) as exc_info:
        Policy(
            id="pii-deny",
            effect="DENY",
            provenance={"source": "operator_asserted", "evidence": "x"},
        )
    assert "extra" in str(exc_info.value).lower() or "provenance" in str(exc_info.value).lower()


def test_bare_string_governed_by_via_model_copy_still_normalizes(graph):
    """Pydantic v2's model_copy(update=...) BYPASSES model_validators, so the
    union-type normalization must live inside ingest, not the model. Drive
    a list[str] through ingest via model_copy — the existing
    test_failed_ingest_does_not_mutate_graph relies on
    exactly this path."""
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file")],
            resources=[ResourceRecord(uri=CSV)],
        )
    )
    good = Governance(
        agents=["planner"],
        policies=[Policy(id="pii-deny", effect="DENY")],
        grants=[AccessGrant(agent="planner", tool="sample::read_file")],
        data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
        governed_by={CSV: ["pii-deny"]},
    )
    assert ingest_governance(good).warnings == []
    # Inject a bare list[str] via model_copy (bypasses validators)
    bumped = good.model_copy(update={"governed_by": {CSV: ["pii-deny"]}})
    assert ingest_governance(bumped).warnings == []  # normalization handles it

    # And the bad shape (typo) is rejected as before
    bad = good.model_copy(update={"governed_by": {CSV: ["dney"]}})
    warnings = ingest_governance(bad).warnings
    assert warnings
    # graph state preserved
    assert queries.check_access("planner", "read_file")["verdict"] == "DENY"


def test_phase2_prunes_authored_only_resource_on_re_ingest(graph):
    """ENV is authored-only (never crawled). Dropping it from the manifest
    must delete the node — otherwise blast_radius answers found:true /
    impacted:[] for a resource nothing governs or touches anymore."""
    crawl_sample(graph)
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
    crawl_sample(graph)
    assert ingest_governance(Governance(agents=["planner"])).warnings == []
    assert queries.blast_radius(CSV)["found"] is True


@pytest.mark.parametrize(
    "content",
    [
        "governed_by: {}\ngoverned_by: {}\n",
        "policies:\n  - id: one\n    id: two\n    effect: DENY\n",
    ],
)
def test_governance_yaml_rejects_duplicate_keys_at_any_depth(tmp_path, content):
    path = tmp_path / "governance.yaml"
    path.write_text(content)

    with pytest.raises(yaml.constructor.ConstructorError, match="duplicate key"):
        load_governance(path)


def test_governance_yaml_rejects_unknown_nested_field(tmp_path):
    path = tmp_path / "governance.yaml"
    path.write_text(
        "policies:\n  - id: deny\n    effect: DENY\n    descriptino: typo\n"
    )

    with pytest.raises(ValidationError, match="policies.0.descriptino"):
        load_governance(path)


def test_ambiguous_bare_name_in_ingest_warns(graph):
    load_crawl("alpha", ["read_file"])
    load_crawl("beta", ["read_file"])
    warnings = ingest_governance(
        Governance(agents=["planner"], grants=[AccessGrant(agent="planner", tool="read_file")])
    ).warnings
    assert any("ambiguous" in w for w in warnings)
    # exact key still resolves
    assert ingest_governance(
        Governance(agents=["planner"], grants=[AccessGrant(agent="planner", tool="alpha::read_file")])
    ).warnings == []


def test_missing_policy_id_warns(graph):
    load_crawl("sample", ["read_file"])
    warnings = ingest_governance(
        Governance(
            policies=[Policy(id="real", effect="DENY")],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["typo-policy"]},
        )
    ).warnings
    assert any("typo-policy" in w and "not defined" in w for w in warnings)
    # the bogus deny edge must not exist -> access is not falsely DENY'd
    assert governance_counts()["governed_by"] == 0


def test_revoking_a_grant_removes_the_edge(graph):
    load_crawl("sample", ["read_file"])
    full = Governance(
        agents=["planner"],
        grants=[AccessGrant(agent="planner", tool="sample::read_file")],
    )
    ingest_governance(full)
    assert governance_counts()["can_call"] == 1

    # re-ingest without the grant -> it is removed
    ingest_governance(Governance(agents=["planner"]))
    assert governance_counts()["can_call"] == 0


def test_failed_ingest_does_not_mutate_graph(graph):
    load_crawl("sample", ["read_file"])
    good = Governance(
        agents=["planner"],
        policies=[Policy(id="deny", effect="DENY")],
        grants=[AccessGrant(agent="planner", tool="sample::read_file")],
        data_access=[DataAccess(tool="sample::read_file", resource="file:///x", mode="READS")],
        governed_by={"file:///x": ["deny"]},
    )
    assert ingest_governance(good).warnings == []
    before = governance_counts()
    assert queries.check_access("planner", "read_file")["verdict"] == "DENY"

    # a manifest with a typo'd policy must be rejected whole — not partially applied
    bad = good.model_copy(update={"governed_by": {"file:///x": ["dney"]}})
    warnings = ingest_governance(bad).warnings
    assert warnings  # rejected
    assert governance_counts() == before  # graph untouched
    assert queries.check_access("planner", "read_file")["verdict"] == "DENY"  # DENY preserved


@pytest.mark.parametrize(
    "tools,resources,policies",
    [(t, r, p) for t in (False, True) for r in (False, True) for p in (False, True)],
)
def test_empty_collection_combinations(backend_graph, tools, resources, policies):
    from toolgraph.models import Policy

    before = queries.graph_state().generation
    loader.load_crawl_result(
        CrawlResult(
            server_name="s",
            tools=[ToolRecord(name="t")] if tools else [],
            resources=[ResourceRecord(uri="file:///r")] if resources else [],
        )
    )
    report = ingest_governance(
        Governance(policies=[Policy(id="p", effect="DENY")] if policies else [])
    )
    assert report.applied
    assert queries.graph_state().generation > before
    loader.load_crawl_result(CrawlResult(server_name="s"))
    assert queries.exposed_tool_contracts() == []
    assert ingest_governance(Governance()).applied
    with driver.session() as session:
        assert session.run("MATCH (p:Policy) RETURN count(p) AS n").single()["n"] == 0
        assert (
            session.run("MATCH ()-[p:PROVIDES]->() RETURN count(p) AS n").single()["n"]
            == 0
        )


def test_revocation_and_failed_ingest_preserve_state(backend_graph, monkeypatch):
    from toolgraph.models import AccessGrant
    from toolgraph.manifest import ingest

    loader.load_crawl_result(CrawlResult(server_name="s", tools=[ToolRecord(name="t")]))
    gov = Governance(agents=["a"], grants=[AccessGrant(agent="a", tool="s::t")])
    assert ingest_governance(gov).applied
    before = queries.graph_state()
    original = ingest.bump_generation

    def fail(tx):
        raise RuntimeError("injected after mutation")

    monkeypatch.setattr(ingest, "bump_generation", fail)
    with pytest.raises(RuntimeError, match="injected"):
        ingest_governance(Governance())
    assert queries.graph_state() == before
    assert queries.agent_exists("a")
    monkeypatch.setattr(ingest, "bump_generation", original)
    assert ingest_governance(Governance()).applied
    assert not queries.agent_exists("a")
    assert queries.graph_state().generation > before.generation


@pytest.mark.parametrize("size", [0, 4096, 4097])
def test_resolution_batches_and_duplicates(backend_graph, size):
    # Most keys need not exist: unknown references must be represented too.
    loader.load_crawl_result(CrawlResult(server_name="a", tools=[ToolRecord(name="same")]))
    loader.load_crawl_result(CrawlResult(server_name="b", tools=[ToolRecord(name="same")]))
    qualified = [f"a::t{i}" for i in range(size)]
    refs = qualified * 2 + (["same", "same", "absent"] if size else [])
    with driver.session() as session:
        counted = CountingSession(session)
        result = queries.resolve_tool_refs(counted, refs)
    assert len(counted.calls) == ((size + 4095) // 4096 + 1 if size else 0)
    assert all(len(next(iter(p.values()))) <= 4096 for p in counted.calls)
    assert all(result[ref] == [] for ref in qualified)
    if size:
        assert result["same"] == ["a::same", "b::same"]
        assert result["absent"] == []
    else:
        assert result == {}


def test_ingest_reuses_resolution_across_manifest_rows(backend_graph):
    _seed_crawl()
    gov = _governance()
    gov.grants *= 100
    gov.data_access *= 100
    with driver.session() as session:
        def apply(tx):
            counted = CountingSession(tx)
            report = ingest._ingest(counted, gov)
            resolution_calls = [p for p in counted.calls if "keys" in p]
            # One resolve query and one drift query; repetition costs neither.
            assert len(resolution_calls) == 2
            assert all(len(p["keys"]) == 2 for p in resolution_calls)
            return report
        assert session.execute_write(apply) == ([], [])
    assert ingest.governance_counts()["can_call"] == 3


def test_ingest_diagnostics_and_rollback(backend_graph):
    _seed_crawl()
    assert ingest.ingest_governance(_governance()).applied
    before = queries.graph_state()
    counts = ingest.governance_counts()
    bad = Governance(grants=[
        AccessGrant(agent="a", tool="missing::first"),
        AccessGrant(agent="a", tool="missing::second"),
        AccessGrant(agent="b", tool="missing::first"),
    ])
    report = ingest.ingest_governance(bad)
    assert len(report.warnings) == 3
    assert "missing::first" in report.warnings[0]
    assert "missing::second" in report.warnings[1]
    assert "missing::first" in report.warnings[2]
    assert queries.graph_state() == before
    assert ingest.governance_counts() == counts

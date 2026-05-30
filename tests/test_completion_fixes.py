"""Behaviors landed in the toolgraph completion round (2026-05-30):

- AGENT_NOT_FOUND verdict on check_access
- blast_radius found:false for unknown nodes
- expected_exceptions validation against declared agents + authored access
- CLI ingest-manifest REJECTED headline on warnings
- governed_by binding shape backward compat (bare list[str] via model_copy)
- Phase 2 prunes orphan Agent + Policy nodes (Codex R3 critique)
- expected_exceptions full reachability invariant (Codex R3 critique)
- AGENT_NOT_FOUND carries found:None (three-state contract per Codex R3.5)
- Policy.extra='forbid' rejects legacy provenance: block
- expected_exceptions against ALLOW policies warned at ingest (Codex R3.5)
- Duplicate policy ids rejected at ingest (Codex R3.5)
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from toolgraph import cli
from toolgraph.graph import driver, loader, queries
from toolgraph.manifest.ingest import governance_counts, ingest_governance
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


def _seed_basic(graph) -> None:
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file"), ToolRecord(name="write_file")],
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


# --- AGENT_NOT_FOUND -----------------------------------------------------


def test_check_access_returns_agent_not_found_for_unknown_agent(graph):
    """Typo'd agent must NOT collapse into NOT_GRANTED — that hides bugs.
    An operator iterating on names needs the distinct verdict to tell
    'this agent has no grant on this tool' from 'this agent doesn't exist'."""
    _seed_basic(graph)
    res = queries.check_access("planer", "read_file")  # typo
    assert res["verdict"] == "AGENT_NOT_FOUND"
    assert res["agent_found"] is False

    # known agent stays unaffected (verdict NOT_GRANTED / DENY / ALLOW)
    res = queries.check_access("planner", "read_file")
    assert res["verdict"] == "DENY"


# --- blast_radius found flag --------------------------------------------


def test_blast_radius_resource_marks_found_false_for_typo(graph):
    _seed_basic(graph)
    res = queries.blast_radius("file:///does/not/exist")
    assert res["kind"] == "resource"
    assert res["found"] is False
    assert res["impacted"] == []

    # real resource: found=True
    res = queries.blast_radius(CSV)
    assert res["found"] is True
    assert res["impacted"], "should report planner's reach to CSV"


def test_blast_radius_policy_marks_found_false_for_typo(graph):
    _seed_basic(graph)
    res = queries.blast_radius("no-such-policy")
    assert res["kind"] == "policy"
    assert res["found"] is False
    assert res["impacted"] == []

    res = queries.blast_radius("pii-deny")
    assert res["found"] is True


# --- expected_exceptions validation -------------------------------------


def test_exception_with_unknown_agent_warns(graph):
    """A typo'd agent in expected_exceptions can't classify any row —
    surface it at ingest, not at audit time."""
    _seed_basic(graph)
    warnings = ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="pii-deny", effect="DENY")],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["pii-deny"]},
            expected_exceptions=[
                ExpectedException(
                    agent="planer",  # typo of 'planner'
                    tool="sample::read_file",
                    policy="pii-deny",
                    resource=CSV,
                    reason="by design",
                )
            ],
        )
    )
    assert warnings
    assert any("planer" in w and "not declared" in w for w in warnings)


def test_exception_narrowed_to_missing_resource_warns(graph):
    """If the (tool, resource) combination isn't authored in this manifest's
    data_access, the exception can't match any deny_evidence row — warn."""
    _seed_basic(graph)
    warnings = ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="pii-deny", effect="DENY")],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["pii-deny"]},
            expected_exceptions=[
                ExpectedException(
                    agent="planner",
                    tool="sample::read_file",
                    policy="pii-deny",
                    resource="file:///wrong",  # not in data_access
                    reason="x",
                )
            ],
        )
    )
    assert warnings
    assert any("file:///wrong" in w and "no READS/WRITES" in w for w in warnings)


# --- CLI REJECTED headline ----------------------------------------------


def test_cli_ingest_manifest_says_rejected_on_warnings(graph, tmp_path):
    """The CLI must NOT say 'Ingested' when ingest rejected the manifest —
    that contradicts the atomicity guarantee directly to the user."""
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file")],
            resources=[ResourceRecord(uri=CSV)],
        )
    )
    manifest_path = tmp_path / "governance.yaml"
    manifest_path.write_text(
        yaml.safe_dump({
            "agents": ["planner"],
            "policies": [{"id": "real", "effect": "DENY"}],
            "grants": [{"agent": "planner", "tool": "sample::read_file"}],
            "governed_by": {CSV: ["typo-policy"]},  # forces rejection
        })
    )
    runner = CliRunner()
    result = runner.invoke(cli.app, ["ingest-manifest", "--governance", str(manifest_path)])
    assert result.exit_code == 1
    assert "REJECTED" in result.output
    assert "graph unchanged" in result.output
    assert "Ingested governance from" not in result.output
    # And the graph was NOT mutated — counts stay zero
    assert governance_counts()["governed_by"] == 0


def test_cli_unsafe_tools_default_filters_to_violations(graph):
    """`toolgraph unsafe-tools agent` defaults to violation-only output;
    --all surfaces authorized_but_governed rows alongside."""
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file"), ToolRecord(name="write_file")],
            resources=[ResourceRecord(uri=CSV)],
        )
    )
    ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="pii-deny", effect="DENY")],
            grants=[
                AccessGrant(agent="planner", tool="sample::read_file"),
                AccessGrant(agent="planner", tool="sample::write_file"),
            ],
            data_access=[
                DataAccess(tool="sample::read_file", resource=CSV, mode="READS"),
                DataAccess(tool="sample::write_file", resource=CSV, mode="WRITES"),
            ],
            governed_by={CSV: ["pii-deny"]},
            expected_exceptions=[
                ExpectedException(
                    agent="planner",
                    tool="sample::read_file",
                    policy="pii-deny",
                    resource=CSV,
                    reason="by design",
                )
            ],
        )
    )
    runner = CliRunner()

    default = runner.invoke(cli.app, ["unsafe-tools", "planner"])
    assert default.exit_code == 0
    assert "write_file" in default.output           # violation
    assert "authorized_but_governed" not in default.output

    full = runner.invoke(cli.app, ["unsafe-tools", "planner", "--all"])
    assert "authorized_but_governed" in full.output
    assert "write_file" in full.output


def test_cli_unsafe_tools_unknown_agent_emits_agent_not_found(graph):
    """A typo'd agent at the CLI must NOT silently return [] — that's the
    exact false-clean the analyzer is supposed to prevent."""
    _seed_basic(graph)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["unsafe-tools", "planer"])
    assert result.exit_code == 1
    assert "AGENT_NOT_FOUND" in result.output


def test_mcp_unsafe_callable_tools_wrapper_filters_by_default(graph):
    """The MCP wrapper must apply the same violation-only default as the CLI
    — agents querying over MCP shouldn't see authorized_but_governed
    rows mixed in."""
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file"), ToolRecord(name="write_file")],
            resources=[ResourceRecord(uri=CSV)],
        )
    )
    ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="pii-deny", effect="DENY")],
            grants=[
                AccessGrant(agent="planner", tool="sample::read_file"),
                AccessGrant(agent="planner", tool="sample::write_file"),
            ],
            data_access=[
                DataAccess(tool="sample::read_file", resource=CSV, mode="READS"),
                DataAccess(tool="sample::write_file", resource=CSV, mode="WRITES"),
            ],
            governed_by={CSV: ["pii-deny"]},
            expected_exceptions=[
                ExpectedException(
                    agent="planner",
                    tool="sample::read_file",
                    policy="pii-deny",
                    resource=CSV,
                    reason="by design",
                )
            ],
        )
    )
    # Direct invocation of the @mcp.tool() function (no MCP transport needed
    # for this assertion — we're testing the wrapper, not the protocol).
    from toolgraph.server.app import unsafe_callable_tools as wrap
    default = wrap.fn("planner") if hasattr(wrap, "fn") else wrap("planner")
    assert default["agent_found"] is True
    assert default["count"] == 1
    assert default["tools"][0]["tool"] == "write_file"
    assert all(t["classification"] == "violation" for t in default["tools"])

    full = wrap.fn("planner", include_authorized=True) if hasattr(wrap, "fn") else wrap("planner", include_authorized=True)
    assert full["count"] == 2
    classifications = {t["classification"] for t in full["tools"]}
    assert classifications == {"violation", "authorized_but_governed"}


def test_mcp_unsafe_callable_tools_wrapper_signals_unknown_agent(graph):
    _seed_basic(graph)
    from toolgraph.server.app import unsafe_callable_tools as wrap
    res = wrap.fn("planer") if hasattr(wrap, "fn") else wrap("planer")
    assert res["agent_found"] is False
    assert res["count"] == 0
    assert res["tools"] == []


def test_check_access_agent_found_is_set_on_every_branch(graph):
    """Callers using ``result['agent_found']`` shouldn't KeyError on the
    success branches just because the field was originally added only for
    the failure case."""
    _seed_basic(graph)
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
            data_access=[
                DataAccess(tool="sample::leak", resource="file:///x", mode="WRITES")
            ],
            governed_by={"sample::leak": ["urn:secret"]},
        )
    )
    res = queries.blast_radius("urn:secret")
    # Re-routed from resource-branch (no match) to policy-branch.
    assert res["kind"] == "policy"
    assert res["found"] is True
    assert any(r["via"] == "tool" for r in res["impacted"])


def test_cli_unbacked_edges_default_excludes_can_call(graph):
    """`toolgraph unbacked-edges` defaults to READS/WRITES/GOVERNED_BY;
    --include-grants widens to CAN_CALL too."""
    _seed_basic(graph)
    runner = CliRunner()

    default = runner.invoke(cli.app, ["unbacked-edges"])
    assert default.exit_code == 0
    assert "CAN_CALL" not in default.output

    wider = runner.invoke(cli.app, ["unbacked-edges", "--include-grants"])
    assert wider.exit_code == 0
    assert "CAN_CALL" in wider.output


# --- backward compat: bare-string governed_by via model_copy ------------


# --- R3: Phase 2 prunes orphan Agent + Policy nodes --------------------


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


# --- R3: stricter expected_exceptions validation -----------------------


def test_exception_with_no_grant_for_tool_warns(graph):
    """Codex R3 critique: an exception that names an agent without a CAN_CALL
    grant on the tool is dead-on-arrival — no unsafe-tools row can ever
    match it. Warn at ingest, not at audit."""
    _seed_basic(graph)
    warnings = ingest_governance(
        Governance(
            agents=["planner", "loner"],  # loner declared, but no grants
            policies=[Policy(id="pii-deny", effect="DENY")],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["pii-deny"]},
            expected_exceptions=[
                ExpectedException(
                    agent="loner",
                    tool="sample::read_file",
                    policy="pii-deny",
                    resource=CSV,
                    reason="will never match",
                )
            ],
        )
    )
    assert warnings
    assert any("loner" in w and "no CAN_CALL grant" in w for w in warnings)


def test_exception_with_wrong_policy_warns(graph):
    """Codex R3 critique: when exc.resource is set but the resource is NOT
    governed_by exc.policy, the exception can never match. Previous round
    only checked (tool, resource) ∈ data_access — missing the (resource,
    policy) binding check."""
    _seed_basic(graph)
    warnings = ingest_governance(
        Governance(
            agents=["planner"],
            policies=[
                Policy(id="pii-deny", effect="DENY"),
                Policy(id="other-deny", effect="DENY"),
            ],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["pii-deny"]},  # NOT other-deny
            expected_exceptions=[
                ExpectedException(
                    agent="planner",
                    tool="sample::read_file",
                    policy="other-deny",  # wrong policy
                    resource=CSV,
                    reason="dead-on-arrival",
                )
            ],
        )
    )
    assert warnings
    assert any(
        "other-deny" in w and "not governed_by" in w for w in warnings
    )


def test_wildcard_exception_with_no_reachable_path_warns(graph):
    """Wildcard exception (resource=None) must still verify the tool reaches
    the policy via SOME path — either a touched resource governed by the
    policy, or tool-level GOVERNED_BY."""
    _seed_basic(graph)
    warnings = ingest_governance(
        Governance(
            agents=["planner"],
            policies=[
                Policy(id="pii-deny", effect="DENY"),
                Policy(id="unrelated-deny", effect="DENY"),
            ],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["pii-deny"]},  # only pii-deny; unrelated-deny has no path
            expected_exceptions=[
                ExpectedException(
                    agent="planner",
                    tool="sample::read_file",
                    policy="unrelated-deny",
                    # resource omitted -> wildcard
                    reason="will never fire",
                )
            ],
        )
    )
    assert warnings
    assert any("dead-on-arrival" in w and "unrelated-deny" in w for w in warnings)


# --- R3: small response-shape + migration fixes ------------------------


def test_check_access_agent_not_found_carries_found_none(graph):
    """README promises every response carries agent_found AND found. For
    AGENT_NOT_FOUND the tool lookup didn't run, so `found` is None (not
    False — hard-coding False would lie about a tool that actually exists).
    Three-state contract; callers should use result.get('found')."""
    _seed_basic(graph)
    res = queries.check_access("planer", "read_file")
    assert res["verdict"] == "AGENT_NOT_FOUND"
    assert res["agent_found"] is False
    assert res["found"] is None


def test_duplicate_policy_id_with_conflicting_effects_is_rejected(graph):
    """Codex R3.5: a manifest with two Policy entries sharing an id (e.g.
    [DENY, ALLOW] for 'legacy') silently overwrites effect on the Policy
    node via MERGE+SET. If the surviving effect is ALLOW, exceptions
    against 'legacy' pass deny-policy validation but then can never
    classify a row. Reject at ingest."""
    _seed_basic(graph)
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
    )
    assert warnings
    assert any("duplicate id" in w and "legacy" in w for w in warnings)


def test_exception_against_allow_policy_warns(graph):
    """Codex R3 final critique: classification only joins to DENY policies,
    so an exception against an ALLOW policy can never fire. Reject at ingest."""
    _seed_basic(graph)
    warnings = ingest_governance(
        Governance(
            agents=["planner"],
            policies=[
                Policy(id="pii-deny", effect="DENY"),
                Policy(id="legacy-allow", effect="ALLOW"),
            ],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["pii-deny", "legacy-allow"]},
            expected_exceptions=[
                ExpectedException(
                    agent="planner",
                    tool="sample::read_file",
                    policy="legacy-allow",  # ALLOW policy — can't ever fire
                    resource=CSV,
                    reason="dead-on-arrival",
                )
            ],
        )
    )
    assert warnings
    assert any("legacy-allow" in w and "ALLOW" in w for w in warnings)


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
    test_review_fixes.test_failed_ingest_does_not_mutate_graph relies on
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
    assert ingest_governance(good) == []
    # Inject a bare list[str] via model_copy (bypasses validators)
    bumped = good.model_copy(update={"governed_by": {CSV: ["pii-deny"]}})
    assert ingest_governance(bumped) == []  # normalization handles it

    # And the bad shape (typo) is rejected as before
    bad = good.model_copy(update={"governed_by": {CSV: ["dney"]}})
    warnings = ingest_governance(bad)
    assert warnings
    # graph state preserved
    assert queries.check_access("planner", "read_file")["verdict"] == "DENY"



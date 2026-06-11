"""ADR-0005 selector surface — the report's regression criteria as tests.

- ambiguous bare names are never auto-selected
- DENY+violation candidates are rejected under strict
- authorized_but_governed: rejected in strict, passes review
- drifted tools are rejected in every profile
- unmapped tools are rejected in strict (and carry the explicit 0.6 penalty)
- batch results preserve candidate input order and stable ids
- risk_score follows the published fixed table
"""

from __future__ import annotations

import json
from importlib.metadata import version

import pytest
from typer.testing import CliRunner

from toolgraph import cli
from toolgraph.graph import loader, selector
from toolgraph.manifest.ingest import ingest_governance
from toolgraph.models import (
    AccessGrant,
    CrawlResult,
    DataAccess,
    ExpectedException,
    Governance,
    Policy,
    Provenance,
    ResourceRecord,
    ToolRecord,
)

REPO = "repo:///work"
SECRETS = "file:///secrets.env"
EXPORT = "export:///sink"

EVIDENCE = Provenance(source="operator_asserted", confidence="high", evidence="runbook.md:1")


def _seed() -> None:
    """One agent, every selector-relevant tool state:

    alpha::git_status   clean ALLOW — granted, mapped w/ evidence, ungoverned
    alpha::read_file    DENY violation (READS secrets governed by pii-deny)
    alpha::export_data  DENY fully covered by an operator exception
    alpha::free_tool    granted but unmapped
    alpha::unbacked     granted, mapped, but READS edge missing evidence
    alpha::ghost_tool   granted, then dropped from the crawl → drifted
    alpha::loose_tool   exposed but no grant
    beta::read_file     makes the bare name 'read_file' ambiguous
    """
    loader.load_crawl_result(
        CrawlResult(
            server_name="alpha",
            transport="stdio",
            tools=[
                ToolRecord(name="git_status", read_only_hint=True),
                ToolRecord(name="read_file"),
                ToolRecord(name="export_data", destructive_hint=True),
                ToolRecord(name="free_tool"),
                ToolRecord(name="unbacked"),
                ToolRecord(name="ghost_tool"),
                ToolRecord(name="loose_tool"),
            ],
            resources=[
                ResourceRecord(uri=REPO),
                ResourceRecord(uri=SECRETS),
                ResourceRecord(uri=EXPORT),
            ],
        )
    )
    loader.load_crawl_result(
        CrawlResult(
            server_name="beta",
            transport="stdio",
            tools=[ToolRecord(name="read_file")],
        )
    )
    report = ingest_governance(
        Governance(
            agents=["planner"],
            policies=[
                Policy(id="pii-deny", effect="DENY"),
                Policy(id="export-deny", effect="DENY"),
            ],
            grants=[
                AccessGrant(agent="planner", tool="alpha::git_status"),
                AccessGrant(agent="planner", tool="alpha::read_file"),
                AccessGrant(agent="planner", tool="alpha::export_data"),
                AccessGrant(agent="planner", tool="alpha::free_tool"),
                AccessGrant(agent="planner", tool="alpha::unbacked"),
                AccessGrant(agent="planner", tool="alpha::ghost_tool"),
            ],
            data_access=[
                DataAccess(
                    tool="alpha::git_status", resource=REPO, mode="READS",
                    provenance=EVIDENCE,
                ),
                DataAccess(
                    tool="alpha::read_file", resource=SECRETS, mode="READS",
                    provenance=EVIDENCE,
                ),
                DataAccess(
                    tool="alpha::export_data", resource=EXPORT, mode="WRITES",
                    provenance=EVIDENCE,
                ),
                DataAccess(tool="alpha::unbacked", resource=REPO, mode="READS"),
            ],
            governed_by={
                SECRETS: [{"policy": "pii-deny", "provenance": EVIDENCE.model_dump()}],
                EXPORT: [{"policy": "export-deny", "provenance": EVIDENCE.model_dump()}],
            },
            expected_exceptions=[
                ExpectedException(
                    agent="planner",
                    tool="alpha::export_data",
                    policy="export-deny",
                    reason="quarterly export is by design",
                )
            ],
        )
    )
    assert report.applied, report.warnings
    # Re-crawl alpha without ghost_tool: the loader keeps the governed node but
    # drops EXPOSES — the drifted state every online profile must reject.
    loader.load_crawl_result(
        CrawlResult(
            server_name="alpha",
            transport="stdio",
            tools=[
                ToolRecord(name="git_status", read_only_hint=True),
                ToolRecord(name="read_file"),
                ToolRecord(name="export_data", destructive_hint=True),
                ToolRecord(name="free_tool"),
                ToolRecord(name="unbacked"),
                ToolRecord(name="loose_tool"),
            ],
            resources=[
                ResourceRecord(uri=REPO),
                ResourceRecord(uri=SECRETS),
                ResourceRecord(uri=EXPORT),
            ],
        )
    )


CANDIDATES = [
    "alpha::git_status",
    "read_file",            # bare name, matches alpha + beta → ambiguous
    "alpha::read_file",
    "alpha::export_data",
    "alpha::ghost_tool",
    "alpha::free_tool",
    "alpha::unbacked",
    "alpha::loose_tool",
    "alpha::no_such_tool",
]


# --- rank_features ---------------------------------------------------------


def test_rank_features_preserves_input_order_and_stable_ids(graph):
    _seed()
    got = selector.rank_features("planner", CANDIDATES)
    assert got["agent_found"] is True
    assert [f["candidate"] for f in got["features"]] == CANDIDATES


def test_rank_features_risk_scores_follow_the_published_table(graph):
    _seed()
    got = selector.rank_features("planner", CANDIDATES)
    by_ref = {f["candidate"]: f for f in got["features"]}
    assert by_ref["alpha::read_file"]["risk_score"] == 1.0      # DENY violation
    assert by_ref["alpha::ghost_tool"]["risk_score"] == 0.8     # drifted
    assert by_ref["alpha::free_tool"]["risk_score"] == 0.6      # unmapped
    assert by_ref["alpha::unbacked"]["risk_score"] == 0.4       # unbacked edge
    assert by_ref["alpha::export_data"]["risk_score"] == 0.2    # authorized_but_governed
    assert by_ref["alpha::git_status"]["risk_score"] == 0.0     # clean
    # No facts to score for unresolved candidates.
    assert by_ref["read_file"]["risk_score"] is None
    assert by_ref["alpha::no_such_tool"]["risk_score"] is None


def test_rank_features_carries_facts_and_annotation_self_claims(graph):
    _seed()
    got = selector.rank_features("planner", ["alpha::read_file", "alpha::export_data"])
    violation, governed = got["features"]
    assert violation["verdict"] == "DENY"
    assert violation["classification"] == "violation"
    assert any("pii-deny" in d["path"] for d in violation["deny_paths"])
    assert governed["classification"] == "authorized_but_governed"
    # #14 annotations enrich features (self-claims, never gates by themselves).
    assert governed["destructive_hint"] is True
    assert violation["destructive_hint"] is None


def test_rank_features_unknown_agent_aborts_selection(graph):
    _seed()
    got = selector.rank_features("nobody", CANDIDATES)
    assert got == {"agent": "nobody", "agent_found": False, "features": []}


def test_rank_features_tool_vanishing_mid_batch_reports_not_found(graph, monkeypatch):
    """Neo4j is read-committed: a concurrent crawl/ingest can delete a tool
    between the resolve query and the facts query. The row must come back as
    a structured TOOL_NOT_FOUND, not crash the batch (Codex review)."""
    _seed()
    real = selector._tool_facts

    def facts_missing_one(s, keys, agent):
        facts = real(s, keys, agent)
        facts.pop("alpha::git_status", None)
        return facts

    monkeypatch.setattr(selector, "_tool_facts", facts_missing_one)
    got = selector.rank_features("planner", ["alpha::git_status", "alpha::free_tool"])
    vanished, survivor = got["features"]
    assert vanished["found"] is False
    assert vanished["verdict"] == "TOOL_NOT_FOUND"
    assert vanished["risk_score"] is None
    assert survivor["candidate"] == "alpha::free_tool"
    assert survivor["found"] is True  # the rest of the batch is unaffected


# --- eligible_tools: the profile matrix ------------------------------------


def _reasons(result: dict) -> dict[str, str]:
    return {r["candidate"]: r["reason"] for r in result["rejected"]}


def test_strict_passes_only_clean_allow(graph):
    _seed()
    got = selector.eligible_tools("planner", CANDIDATES, profile="strict")
    assert got["eligible"] == ["alpha::git_status", "alpha::unbacked"]
    assert _reasons(got) == {
        "read_file": "AMBIGUOUS_TOOL",
        "alpha::read_file": "DENY_VIOLATION",
        "alpha::export_data": "DENY_GOVERNED",
        "alpha::ghost_tool": "DRIFTED",
        "alpha::free_tool": "UNMAPPED",
        "alpha::loose_tool": "NOT_GRANTED",
        "alpha::no_such_tool": "TOOL_NOT_FOUND",
    }


def test_review_passes_exceptions_and_unmapped_but_not_violations(graph):
    _seed()
    got = selector.eligible_tools("planner", CANDIDATES, profile="review")
    assert got["eligible"] == [
        "alpha::git_status",
        "alpha::export_data",
        "alpha::free_tool",
        "alpha::unbacked",
    ]
    assert _reasons(got)["alpha::read_file"] == "DENY_VIOLATION"


def test_explore_rejects_only_resolution_grant_and_drift(graph):
    _seed()
    got = selector.eligible_tools("planner", CANDIDATES, profile="explore")
    assert got["eligible"] == [
        "alpha::git_status",
        "alpha::read_file",  # violation passes in the offline sandbox profile
        "alpha::export_data",
        "alpha::free_tool",
        "alpha::unbacked",
    ]
    assert set(_reasons(got)) == {
        "read_file", "alpha::ghost_tool", "alpha::loose_tool", "alpha::no_such_tool",
    }


@pytest.mark.parametrize("profile", sorted(selector.PROFILES))
def test_ambiguous_bare_name_is_never_auto_selected(graph, profile):
    _seed()
    got = selector.eligible_tools("planner", ["read_file"], profile=profile)
    assert got["eligible"] == []
    [row] = got["rejected"]
    assert row["reason"] == "AMBIGUOUS_TOOL"
    assert row["candidates"] == ["alpha::read_file", "beta::read_file"]


@pytest.mark.parametrize("profile", sorted(selector.PROFILES))
def test_drifted_tools_are_rejected_in_every_profile(graph, profile):
    _seed()
    got = selector.eligible_tools("planner", ["alpha::ghost_tool"], profile=profile)
    assert got["eligible"] == []
    assert got["rejected"][0]["reason"] == "DRIFTED"


def test_deny_rejections_carry_policy_evidence_paths(graph):
    _seed()
    got = selector.eligible_tools("planner", ["alpha::read_file"], profile="strict")
    [row] = got["rejected"]
    assert row["paths"] == [
        f"(read_file) -READS-> ({SECRETS}) -GOVERNED_BY-> (pii-deny:DENY)"
    ]


def test_unknown_profile_raises(graph):
    _seed()
    with pytest.raises(ValueError, match="unknown profile"):
        selector.eligible_tools("planner", CANDIDATES, profile="production")


# --- selection_explain ------------------------------------------------------


def test_selection_explain_eligible_tool(graph):
    _seed()
    got = selector.selection_explain("planner", "alpha::git_status")
    assert got["decision"] == "eligible"
    assert got["risk_score"] == 0.0
    assert "agent has CAN_CALL grant" in got["reasons"]
    assert "no DENY policy path found" in got["reasons"]
    assert "server self-claim (crawled/medium): read_only_hint=True" in got["reasons"]


def test_selection_explain_rejected_tool_names_the_deny_path(graph):
    _seed()
    got = selector.selection_explain("planner", "alpha::read_file")
    assert got["decision"] == "rejected"
    assert any("pii-deny" in r and "[violation]" in r for r in got["reasons"])


def test_selection_explain_surfaces_operator_exception_reason(graph):
    _seed()
    got = selector.selection_explain("planner", "alpha::export_data", profile="review")
    assert got["decision"] == "eligible"
    assert any("quarterly export is by design" in r for r in got["reasons"])


# --- CLI contract (ADR-0003) -------------------------------------------------


runner = CliRunner()

# click >= 8.2 always splits streams; older click folds stderr into stdout,
# making purity unassertable (same guard as test_cli_contract).
CLICK_SPLITS_STREAMS = tuple(
    int(p) for p in version("click").split(".")[:2]
) >= (8, 2)


def test_cli_eligible_tools_stdout_is_pure_json(graph):
    _seed()
    result = runner.invoke(
        cli.app, ["eligible-tools", "planner", *CANDIDATES, "--profile", "review"]
    )
    assert result.exit_code == 0
    got = json.loads(result.stdout)
    assert got == selector.eligible_tools("planner", CANDIDATES, profile="review")


def test_cli_selector_commands_fail_loud_on_unknown_agent(graph):
    _seed()
    for cmd in (
        ["rank-features", "nobody", "alpha::git_status"],
        ["eligible-tools", "nobody", "alpha::git_status"],
        ["selection-explain", "nobody", "alpha::git_status"],
    ):
        result = runner.invoke(cli.app, cmd)
        assert result.exit_code == 1, cmd
        if CLICK_SPLITS_STREAMS:
            assert "AGENT_NOT_FOUND" in result.stderr
            assert result.stdout == ""


def test_cli_rejects_unknown_profile(graph):
    _seed()
    result = runner.invoke(
        cli.app, ["eligible-tools", "planner", "alpha::git_status", "--profile", "prod"]
    )
    assert result.exit_code == 2
    if CLICK_SPLITS_STREAMS:
        assert "unknown profile" in result.stderr

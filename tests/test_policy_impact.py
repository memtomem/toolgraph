"""Real-backend semantic parity, immutable source, and executable report contracts."""

from __future__ import annotations

import copy
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from typer.testing import CliRunner

from tests._helpers import graph_content
from toolgraph import cli, config
from toolgraph.artifacts import canonical_json_bytes
from toolgraph.graph import driver, loader, queries, selector
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
from toolgraph.policy_impact import (
    PolicyImpactError,
    build_policy_impact,
    check_policy_impact,
    impact_markdown,
    load_impact,
)
from toolgraph.policy_impact_models import ImpactReport

NOW = datetime(2026, 10, 16, tzinfo=UTC)
SHARED = "file:///shared/private.txt"
OTHER = "file:///other.txt"
CANDIDATES = ["one::read", "two::read", "one::write"]


def seed():
    for server, tools in [("one", ["read", "write"]), ("two", ["read"])]:
        loader.load_crawl_result(
            CrawlResult(
                server_name=server,
                tools=[ToolRecord(name=t) for t in tools],
                resources=[ResourceRecord(uri=SHARED)],
            )
        )
    gov = Governance(
        agents=["a", "b"],
        policies=[Policy(id="p", effect="ALLOW")],
        grants=[AccessGrant(agent="a", tool=t) for t in CANDIDATES]
        + [AccessGrant(agent="b", tool="one::read")],
        data_access=[
            DataAccess(
                tool=t,
                resource=SHARED if t.endswith("read") else OTHER,
                mode="READS" if t.endswith("read") else "WRITES",
            )
            for t in CANDIDATES
        ],
        governed_by={SHARED: ["p"]},
    )
    assert ingest_governance(gov).applied
    return gov


def impact(gov, **kwargs):
    return build_policy_impact(
        gov,
        agent=kwargs.pop("agent", "a"),
        candidates=kwargs.pop("candidates", CANDIDATES),
        created_at=NOW,
        **kwargs,
    )


def signature(rows):
    return [(r["candidate"], r["after"]["decision"], r["after"]["reason"]) for r in rows]


@pytest.mark.parametrize("profile", ["strict", "review", "explore"])
@pytest.mark.parametrize(
    "change", ["grant", "deny", "direct", "exceptions", "effects", "agent", "duplicates"]
)
def test_preview_matches_real_ingest_without_source_writes(backend_graph, change, profile):
    gov = seed()
    if change == "grant":
        gov.grants = [g for g in gov.grants if g.tool != "one::read"]
    elif change == "deny":
        gov.policies[0].effect = "DENY"
    elif change == "direct":
        gov.policies[0].effect = "DENY"
        gov.governed_by["one::write"] = ["p"]
    elif change == "exceptions":
        gov.policies[0].effect = "DENY"
        gov.expected_exceptions = [
            ExpectedException(agent="a", tool="one::read", policy="p", reason="wildcard"),
            ExpectedException(
                agent="a", tool="one::read", policy="p", resource=SHARED, reason="specific"
            ),
        ]
    elif change == "effects":
        gov.data_access = [d for d in gov.data_access if d.tool != "one::write"]
    elif change == "agent":
        gov.agents = ["b"]
        gov.grants = [g for g in gov.grants if g.agent == "b"]
    elif change == "duplicates":
        gov.policies[0].effect = "DENY"
        gov.data_access += [
            DataAccess(
                tool="one::read",
                resource=SHARED,
                mode="READS",
                provenance=Provenance(evidence="last effect"),
            )
        ]
        gov.governed_by["one::read"] = [
            GovernedByBinding(policy="p", provenance=Provenance(evidence="first"))
        ]
        gov.governed_by[SHARED] = [
            "p",
            GovernedByBinding(policy="p", provenance=Provenance(evidence="last binding")),
        ]
    state, contents = queries.graph_state(), graph_content()
    report = impact(gov, profile=profile)
    assert queries.graph_state() == state
    assert graph_content() == contents
    assert report == impact(gov, profile=profile)
    assert ingest_governance(gov).applied
    # Re-run the same pure comparison on the ACTUALLY changed graph. Its before
    # side is assembled from real selector queries, not the hypothetical evaluator.
    observed = impact(gov, profile=profile)
    selected = selector.eligible_tools("a", sorted(CANDIDATES), profile)
    if selected["agent_found"]:
        for row in report["rows"]:
            assert (row["after"]["decision"] == "eligible") == (
                row["candidate"] in selected["eligible"]
            )
    assert [r["after"] for r in report["rows"]] == [r["before"] for r in observed["rows"]]
    if report["regression_ready"]:
        assert check_policy_impact(ImpactReport.model_validate(report))["passed"]
    else:
        with pytest.raises(PolicyImpactError, match="unresolved"):
            check_policy_impact(ImpactReport.model_validate(report))
    if change == "deny" and profile != "explore":
        assert sum(r["change"] == "newly_rejected" for r in report["rows"]) == 2
    if change == "exceptions":
        row = next(r for r in report["rows"] if r["candidate"] == "one::read")
        assert row["after"]["evidence"][0]["exception_reason"] == "specific"


def test_added_agent_and_ambiguous_candidates_remain_visible(backend_graph):
    gov = seed()
    gov.agents.append("new")
    gov.grants.append(AccessGrant(agent="new", tool="one::read"))
    report = impact(gov, agent="new", candidates=["read", "missing", "one::read"])
    assert len(report["rows"]) == 3
    assert all(r["change"] == "unresolved" for r in report["rows"])
    assert not report["regression_ready"]
    assert (
        next(r for r in report["rows"] if r["candidate"] == "one::read")["after"]["decision"]
        == "eligible"
    )


def test_drift_and_pruning_match_actual_ingest(backend_graph):
    gov = seed()
    loader.retire_unlisted_servers({"two"})
    state, contents = queries.graph_state(), graph_content()
    unchanged = impact(gov)
    assert any(r["after"]["reason"] == "DRIFTED" for r in unchanged["rows"])
    with pytest.raises(PolicyImpactError, match="candidate manifest rejected"):
        impact(gov, strict_drift=True)
    gov.grants = [g for g in gov.grants if not g.tool.startswith("one::")]
    gov.data_access = [d for d in gov.data_access if not d.tool.startswith("one::")]
    report = impact(gov)
    assert queries.graph_state() == state and graph_content() == contents
    assert all(
        r["after"]["reason"] == "TOOL_NOT_FOUND"
        for r in report["rows"]
        if r["candidate"].startswith("one::")
    )
    assert ingest_governance(gov).applied
    observed = impact(gov)
    assert [r["after"] for r in report["rows"]] == [r["before"] for r in observed["rows"]]


def test_grant_addition_and_regression_gate(backend_graph):
    full = seed()
    base = full.model_copy(deep=True)
    base.grants = [g for g in base.grants if g.tool != "one::write"]
    assert ingest_governance(base).applied
    report = impact(full)
    assert any(r["change"] == "newly_eligible" for r in report["rows"])
    model = ImpactReport.model_validate(report)
    state, contents = queries.graph_state(), graph_content()
    assert not check_policy_impact(model)["passed"]
    assert queries.graph_state() == state and graph_content() == contents
    assert ingest_governance(full).applied
    assert check_policy_impact(model)["passed"]
    # Compatible generation/governance changes need not exactly match a digest.
    full.policies.append(Policy(id="unrelated", effect="ALLOW"))
    assert ingest_governance(full).applied
    assert check_policy_impact(model)["passed"]


def test_invalid_candidate_and_budgets_preserve_source(backend_graph, monkeypatch):
    import toolgraph.policy_impact as module

    gov = seed()
    contents, state = graph_content(), queries.graph_state()
    bad = gov.model_copy(deep=True)
    bad.grants.append(AccessGrant(agent="a", tool="missing"))
    with pytest.raises(PolicyImpactError, match="rejected"):
        impact(bad)
    for bound in ["MAX_PROJECTION_ROWS", "MAX_IMPACT_BYTES"]:
        with monkeypatch.context() as patch:
            patch.setattr(module, bound, 1)
            with pytest.raises(PolicyImpactError, match="budget"):
                impact(gov)
    assert contents == graph_content() and state == queries.graph_state()


def test_scope_validation_precedes_database_access(monkeypatch):
    import toolgraph.policy_impact as module

    def forbidden():
        pytest.fail("database opened")

    monkeypatch.setattr(module, "acquire_readonly_session", forbidden)
    for refs in [[], ["one::read"] * 4097, ["Bearer abc123"], ["x\ncmd"]]:
        with pytest.raises(ValueError):
            impact(Governance(), candidates=refs)
    with pytest.raises(ValueError):
        impact(Governance(), profile="invalid")


def test_missing_database_not_created(tmp_path, monkeypatch):
    path = tmp_path / "absent" / "graph.lbug"
    monkeypatch.setattr(config, "settings", config.Settings(backend="ladybug", db_path=path))
    driver.close_driver()
    with pytest.raises(driver.BackendUnavailableError):
        impact(Governance())
    assert not path.parent.exists()


def test_artifact_schema_privacy_and_cli(backend_graph, tmp_path, artifact_format_checker):
    gov = seed()
    gov.policies[0].effect = "DENY"
    gov.data_access[0].provenance = Provenance(
        evidence="https://host/path?token=secret Bearer abc123"
    )
    report = impact(gov)
    schema = json.loads(
        (Path(__file__).parents[1] / "contracts/policy-impact.schema.json").read_text()
    )
    Draft202012Validator(schema, format_checker=artifact_format_checker).validate(report)
    encoded = canonical_json_bytes(report)
    assert b"abc123" not in encoded and b"token=secret" not in encoded
    assert b"file:///shared/private.txt" in encoded
    path = tmp_path / "impact.json"
    path.write_bytes(encoded)
    assert load_impact(path).model_dump(mode="json") == report
    assert "one::read" in impact_markdown(report)
    manifest = tmp_path / "candidate.yaml"
    manifest.write_text(yaml.safe_dump(gov.model_dump(mode="json")))
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "policy",
            "impact",
            str(manifest),
            *CANDIDATES,
            "--agent",
            "a",
            "--format",
            "json",
            "--output",
            str(path),
        ],
    )
    assert result.exit_code == 0, result.output
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert runner.invoke(cli.app, ["policy", "check", str(path)]).exit_code == 1
    assert ingest_governance(gov).applied
    assert runner.invoke(cli.app, ["policy", "check", str(path)]).exit_code == 0
    original = manifest.read_bytes()
    failed = runner.invoke(
        cli.app,
        ["policy", "impact", str(manifest), *CANDIDATES, "--agent", "a", "--output", str(manifest)],
    )
    assert failed.exit_code == 1 and manifest.read_bytes() == original


def test_artifact_tampering_and_incompatible_catalog(backend_graph, tmp_path):
    gov = seed()
    report = impact(gov)
    path = tmp_path / "impact.json"
    for mutate in [
        lambda x: x["rows"].pop(),
        lambda x: x.update(profile="unknown"),
        lambda x: x.pop("schema_version"),
        lambda x: x.pop("kind"),
        lambda x: x.pop("mode"),
        lambda x: x.pop("complete"),
        lambda x: x.update(schema_version=True),
        lambda x: x.update(complete=1),
        lambda x: x["rows"][0]["after"].update(reason="DENY_VIOLATION"),
    ]:
        broken = copy.deepcopy(report)
        mutate(broken)
        path.write_text(json.dumps(broken))
        with pytest.raises(PolicyImpactError, match="invalid"):
            load_impact(path)
    path.write_text('{"kind": "a", "kind": "b"}')
    with pytest.raises(PolicyImpactError):
        load_impact(path)
    loader.load_crawl_result(
        CrawlResult(
            server_name="one",
            tools=[ToolRecord(name="read", description="changed"), ToolRecord(name="write")],
        )
    )
    with pytest.raises(PolicyImpactError, match="incompatible"):
        check_policy_impact(ImpactReport.model_validate(report))


def test_unstable_state_never_publishes_result(backend_graph, monkeypatch):
    gov = seed()
    original = queries.graph_state
    n = 0

    def changing(s=None):
        nonlocal n
        state = original(s)
        n += 1
        return queries.GraphState(state.instance_id, state.generation + n, state.governance_digest)

    monkeypatch.setattr(queries, "graph_state", changing)
    monkeypatch.setattr(queries, "_bracket_backoff", lambda _: None)
    with pytest.raises(queries.GraphStateChurnError):
        impact(gov)


def test_deny_removal_changes_decisions_without_changing_graph_size(backend_graph):
    gov = seed()
    gov.policies[0].effect = "DENY"
    assert ingest_governance(gov).applied
    before_counts = loader.graph_counts()
    gov.policies[0].effect = "ALLOW"
    result = impact(gov)
    assert sum(r["change"] == "newly_eligible" for r in result["rows"]) == 2
    assert ingest_governance(gov).applied
    assert loader.graph_counts() == before_counts
    assert check_policy_impact(ImpactReport.model_validate(result))["passed"]


def test_evidence_only_change_and_untrusted_markdown(backend_graph):
    from toolgraph.policy_impact import _display

    gov = seed()
    gov.policies[0].effect = "DENY"
    assert ingest_governance(gov).applied
    gov.data_access[0].provenance = Provenance(evidence="new evidence")
    report = impact(gov)
    row = next(r for r in report["rows"] if r["candidate"] == "one::read")
    assert row["change"] == "unchanged" and row["details_changed"]
    text = _display("[click](https://host)<script>bad</script>*bold*")
    assert "[click](" not in text and "<script>" not in text and "*bold*" not in text


@pytest.mark.parametrize("field,value", [("instance_id", None), ("governance_digest", None)])
def test_missing_provenance_is_not_a_successful_preview(backend_graph, field, value):
    gov = seed()
    with driver.session() as s:
        s.run(f"MATCH (m:GraphMeta) SET m.{field}=$value", value=value)
    before = graph_content()
    with pytest.raises(PolicyImpactError, match="instance id"):
        impact(gov)
    assert graph_content() == before


def test_catalog_reset_requires_new_expectations(backend_graph):
    gov = seed()
    report = ImpactReport.model_validate(impact(gov))
    with driver.session() as s:
        s.run("MATCH (m:GraphMeta) SET m.instance_id='other-instance'")
    before = graph_content()
    with pytest.raises(PolicyImpactError, match="incompatible"):
        check_policy_impact(report)
    assert graph_content() == before


def test_retry_restarts_the_whole_projection(backend_graph, monkeypatch):
    gov = seed()
    original = queries.graph_state
    calls = 0

    def once(s=None):
        nonlocal calls
        calls += 1
        state = original(s)
        # First bracket's post-read only moves; the second attempt is stable.
        if calls == 3:
            return queries.GraphState(
                state.instance_id, state.generation + 1, state.governance_digest
            )
        return state

    monkeypatch.setattr(queries, "graph_state", once)
    monkeypatch.setattr(queries, "_bracket_backoff", lambda _: None)
    before = graph_content()
    assert impact(gov)["complete"]
    assert calls == 6
    assert graph_content() == before


def test_private_output_alias_and_failed_preview_do_not_replace_files(backend_graph, tmp_path):
    gov = seed()
    manifest = tmp_path / "candidate.yaml"
    manifest.write_text(yaml.safe_dump(gov.model_dump(mode="json")))
    alias = tmp_path / "alias.yaml"
    os.link(manifest, alias)
    args = ["policy", "impact", str(manifest), "one::read", "--agent", "a", "--output"]
    runner = CliRunner()
    original = manifest.read_bytes()
    assert runner.invoke(cli.app, [*args, str(alias)]).exit_code == 1
    assert manifest.read_bytes() == original
    target = tmp_path / "existing.json"
    target.write_text("keep")
    gov.grants.append(AccessGrant(agent="a", tool="missing"))
    manifest.write_text(yaml.safe_dump(gov.model_dump(mode="json")))
    assert runner.invoke(cli.app, [*args, str(target)]).exit_code == 1
    assert target.read_text() == "keep"


def test_expected_artifact_evidence_is_scrubbed_on_check(backend_graph, tmp_path):
    gov = seed()
    report = impact(gov)
    report["rows"][0]["after"]["evidence"] = [{"path": "Bearer planted-secret"}]
    # Evidence is non-executable, but it still must not leak into a new export.
    report["rows"][0]["details_changed"] = True
    result = check_policy_impact(ImpactReport.model_validate(report))
    assert result["passed"]
    assert "planted-secret" not in json.dumps(result)


def test_schema_stays_in_sync_with_runtime_model():
    schema = json.loads(
        (Path(__file__).parents[1] / "contracts/policy-impact.schema.json").read_text()
    )
    generated = ImpactReport.model_json_schema()
    for key, value in generated.items():
        assert schema[key] == value


def test_projection_limit_accepts_exact_boundary_and_rejects_overflow():
    from toolgraph.policy_impact import MAX_IMPACT_BYTES, MAX_PROJECTION_ROWS, _Projection

    budget = _Projection(None)
    payload = {"value": "data"}
    budget.rows = MAX_PROJECTION_ROWS - 1
    budget.bytes = MAX_IMPACT_BYTES - len(canonical_json_bytes(payload))
    budget.account(payload)
    with pytest.raises(PolicyImpactError, match="budget"):
        budget.account(payload)


def test_markdown_preserves_entities_and_contains_hostile_evidence():
    from toolgraph.policy_impact import _display, _json_block, check_markdown

    displayed = _display("DRIFT: tool 'one::read' & <script># bad")
    assert "'one::read'" in displayed
    assert "&amp;" in displayed and "&lt;script&gt;" in displayed
    assert "&\\#" not in displayed and "<script>" not in displayed
    evidence = {"path": "read_note -> file:///x", "reason": "```\n<script>bad</script>\n````"}
    block = _json_block(evidence)
    lines = block.splitlines()
    assert lines[0] == "`````json" and lines[-1] == "`````"
    assert json.loads("\n".join(lines[1:-1])) == evidence
    rendered = check_markdown(
        {
            "passed": False,
            "expected_governance_digest": "a" * 64,
            "current_governance_digest": "b" * 64,
            "rows": [
                {
                    "candidate": "host::a|<script>",
                    "matches": False,
                    "expected": {"reason": None, "decision": "eligible"},
                    "actual": {"reason": "NOT_GRANTED", "decision": "rejected"},
                }
            ],
        }
    )
    assert rendered.startswith("# Policy regression check: FAIL")
    assert "a" * 64 in rendered and "b" * 64 in rendered
    assert "| Candidate |" in rendered and "NOT_GRANTED" in rendered
    assert "a\\|&lt;script&gt;" in rendered

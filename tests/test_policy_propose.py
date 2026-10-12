"""Tests for toolgraph policy propose with graph-grounded grant resolution."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from toolgraph import cli
from toolgraph.graph import driver, schema
from toolgraph.manifest.parser import governance_digest, load_governance
from toolgraph.policy_propose import (
    StateChurnError,
    StateMismatchError,
    propose,
    resolve_grants_from_graph,
    run_propose,
)


@pytest.fixture
def sample_gov_path(tmp_path) -> Path:
    p = tmp_path / "governance.yaml"
    p.write_text(
        """
agents:
  - ops-agent
  - public-bot
grants:
  - agent: ops-agent
    tool: filesystem::read_file
    granted_by: security-team
  - agent: ops-agent
    tool: filesystem::write_file
    granted_by: security-team
"""
    )
    return p


@pytest.fixture
def sample_observed_jsonl(tmp_path, sample_gov_path) -> Path:
    gov = load_governance(sample_gov_path)
    digest = governance_digest(gov)
    p = tmp_path / "observed.jsonl"
    lines = [
        json.dumps({
            "envelope": {
                "governance_digest": digest,
                "graph_generation": 1,
                "graph_instance_id": "test-instance",
                "profile": "strict",
                "window_start": "2026-08-01T00:00:00Z",
                "window_end": "2026-08-31T23:59:59Z",
            }
        }),
        # 1. Un-granted and called -> additions candidate
        json.dumps({
            "agent": "public-bot",
            "tool_key": "filesystem::read_file",
            "decision": "rejected",
            "calls": 12,
            "would_block_calls": 12,
            "reject_reasons": ["NOT_GRANTED"],
        }),
        # 2. Deny governed -> conflicts candidate
        json.dumps({
            "agent": "public-bot",
            "tool_key": "filesystem::write_file",
            "decision": "rejected",
            "calls": 5,
            "would_block_calls": 5,
            "reject_reasons": ["NOT_GRANTED", "DENY_GOVERNED"],
        }),
        # 3. Granted and 0 calls -> removals candidate
        json.dumps({
            "agent": "ops-agent",
            "tool_key": "filesystem::write_file",
            "decision": "eligible",
            "calls": 0,
            "would_block_calls": 0,
            "reject_reasons": [],
        }),
    ]
    p.write_text("\n".join(lines) + "\n")
    return p


def test_deny_invariant_never_proposed_as_addition():
    gov = {
        "agents": ["bot"],
        "grants": [],
    }
    observed = [
        {
            "agent": "bot",
            "ref": "filesystem::write_file",
            "has_tool_key": True,
            "decision": "rejected",
            "calls": 10,
            "would_block_calls": 10,
            "reasons": ["NOT_GRANTED", "DENY_GOVERNED"],
            "reasons_complete": True,
        }
    ]
    res = propose(gov, observed, min_would_block=1, granted={}, ambiguous_grants=[])
    assert len(res["additions"]) == 0
    assert len(res["conflicts"]) == 1
    assert res["conflicts"][0]["tool"] == "filesystem::write_file"


def test_ambiguous_bare_grant_protection_regression(backend_graph, tmp_path):
    """Regression test: bare grant in manifest must not be mistakenly removed

    when multiple servers provide that tool name, even if observations report
    zero calls on one of the servers.
    """
    schema.init_schema()
    with driver.session() as s:
        # Populate live graph with two servers exposing read_file
        s.run("CREATE (m1:MCPServer {name: 'alpha'}) CREATE (t1:Tool {key: 'alpha::read_file', name: 'read_file'}) CREATE (m1)-[:EXPOSES]->(t1)")
        s.run("CREATE (m2:MCPServer {name: 'beta'}) CREATE (t2:Tool {key: 'beta::read_file', name: 'read_file'}) CREATE (m2)-[:EXPOSES]->(t2)")

        # Bare grant in governance: agent 'worker' has tool 'read_file'
        grants = [{"agent": "worker", "tool": "read_file"}]
        resolved, ambiguous = resolve_grants_from_graph(s, grants)

        # It matches 2 tools in live graph -> must be flagged as AMBIGUOUS_GRANT
        assert len(ambiguous) == 1
        assert ambiguous[0]["reason"] == "AMBIGUOUS_GRANT"
        assert "alpha::read_file" in ambiguous[0]["note"]
        assert "beta::read_file" in ambiguous[0]["note"]
        # It must NOT be added to resolved grants as a single server mapping
        assert ("worker", "beta::read_file") not in resolved
        assert ("worker", "alpha::read_file") not in resolved

        # If observation window reports beta::read_file with 0 calls
        observed = [{
            "agent": "worker",
            "ref": "beta::read_file",
            "has_tool_key": True,
            "decision": "eligible",
            "calls": 0,
            "would_block_calls": 0,
            "reasons": [],
            "reasons_complete": True,
        }]
        gov = {"agents": ["worker"], "grants": grants}
        res = propose(gov, observed, min_would_block=1, granted=resolved, ambiguous_grants=ambiguous)

        # Crucial check: removals must be EMPTY. Neither read_file nor beta::read_file is removed!
        assert res["removals"] == []
        assert len(res["insufficient_evidence"]) == 1
        assert res["insufficient_evidence"][0]["reason"] == "AMBIGUOUS_GRANT"


def test_run_propose_composite_token_validation(backend_graph, sample_gov_path, sample_observed_jsonl):
    schema.init_schema()
    gov = load_governance(sample_gov_path)
    digest = governance_digest(gov)

    # 1. Update live graph GraphMeta to match envelope
    with driver.session() as s:
        s.run("CREATE (t:Tool {key: 'filesystem::read_file', name: 'read_file'})")
        s.run("CREATE (t:Tool {key: 'filesystem::write_file', name: 'write_file'})")
        s.run(
            "MERGE (m:GraphMeta {id: 'singleton'}) "
            "SET m.instance_id = 'test-instance', m.generation = 1, m.governance_digest = $d",
            d=digest,
        )

    # Exact token match -> should succeed
    proposal, rendered = run_propose(sample_gov_path, sample_observed_jsonl, min_would_block=3)
    assert len(proposal["additions"]) == 1
    assert proposal["additions"][0]["tool"] == "filesystem::read_file"
    assert len(proposal["conflicts"]) == 1
    assert len(proposal["removals"]) == 1
    assert "exact composite token matched: test-instance, gen 1" in rendered


def test_run_propose_instance_mismatch(backend_graph, sample_gov_path, sample_observed_jsonl):
    schema.init_schema()
    gov = load_governance(sample_gov_path)
    digest = governance_digest(gov)

    with driver.session() as s:
        s.run(
            "MERGE (m:GraphMeta {id: 'singleton'}) "
            "SET m.instance_id = 'live-instance', m.generation = 1, m.governance_digest = $d",
            d=digest,
        )

    with pytest.raises(StateMismatchError, match="graph instance mismatch"):
        run_propose(sample_gov_path, sample_observed_jsonl)


def test_run_propose_generation_shift_and_revalidation(backend_graph, sample_gov_path, sample_observed_jsonl):
    schema.init_schema()
    gov = load_governance(sample_gov_path)
    digest = governance_digest(gov)

    with driver.session() as s:
        s.run("CREATE (t:Tool {key: 'filesystem::read_file', name: 'read_file'})")
        s.run("CREATE (t:Tool {key: 'filesystem::write_file', name: 'write_file'})")
        s.run(
            "MERGE (m:GraphMeta {id: 'singleton'}) "
            "SET m.instance_id = 'test-instance', m.generation = 2, m.governance_digest = $d",
            d=digest,
        )

    # Generation 2 vs envelope generation 1 without --revalidate-current -> StateMismatchError
    with pytest.raises(StateMismatchError, match="export declared graph_generation 1 but live graph generation is 2"):
        run_propose(sample_gov_path, sample_observed_jsonl, revalidate_current=False)

    # Strict revalidation needs a live agent, exposure, and authored data flow.
    with driver.session() as s:
        s.run("CREATE (:Agent {id:'public-bot'})")
        s.run("MATCH (t:Tool {key:'filesystem::read_file'}) "
              "CREATE (m:MCPServer {name:'filesystem'}) CREATE (m)-[:EXPOSES]->(t) "
              "CREATE (r:Resource {uri:'file:///data'}) CREATE (t)-[:READS]->(r)")

    # With revalidate_current=True -> succeeds with distinguishing header
    proposal, rendered = run_propose(sample_gov_path, sample_observed_jsonl, revalidate_current=True)
    assert len(proposal["additions"]) == 1
    assert "revalidated against current catalog" in rendered
    assert "observed: instance=test-instance, gen=1" in rendered
    assert "current: instance=test-instance, gen=2" in rendered


def test_state_churn_exhaustion_typed_refusal(backend_graph, sample_gov_path, sample_observed_jsonl):
    schema.init_schema()
    gov = load_governance(sample_gov_path)
    digest = governance_digest(gov)

    with driver.session() as s:
        s.run(
            "MERGE (m:GraphMeta {id: 'singleton'}) "
            "SET m.instance_id = 'test-instance', m.generation = 1, m.governance_digest = $d",
            d=digest,
        )

    # Simulate 5-attempt churn exhaustion
    from toolgraph.graph import queries

    churn = queries.GraphStateChurnError("graph state changed during every read attempt")
    with patch("toolgraph.graph.queries.with_graph_state", side_effect=churn):
        with pytest.raises(StateChurnError) as raised:
            run_propose(sample_gov_path, sample_observed_jsonl)
    assert str(raised.value) == (
        "graph state churn during observation proposal: concurrent graph "
        "modifications exhausted 5 read attempts"
    )

    # Any other RuntimeError is not churn and must surface unchanged, whatever
    # its text says.
    other = RuntimeError("graph state changed during every read attempt")
    with patch("toolgraph.graph.queries.with_graph_state", side_effect=other):
        with pytest.raises(RuntimeError) as raised:
            run_propose(sample_gov_path, sample_observed_jsonl)
    assert raised.value is other


def test_cli_policy_propose(backend_graph, sample_gov_path, sample_observed_jsonl):
    schema.init_schema()
    gov = load_governance(sample_gov_path)
    digest = governance_digest(gov)

    with driver.session() as s:
        s.run("CREATE (t:Tool {key: 'filesystem::read_file', name: 'read_file'})")
        s.run("CREATE (t:Tool {key: 'filesystem::write_file', name: 'write_file'})")
        s.run(
            "MERGE (m:GraphMeta {id: 'singleton'}) "
            "SET m.instance_id = 'test-instance', m.generation = 1, m.governance_digest = $d",
            d=digest,
        )

    runner = CliRunner()
    # 1. Standard markdown to stdout
    result = runner.invoke(cli.app, ["policy", "propose", str(sample_gov_path), str(sample_observed_jsonl)])
    assert result.exit_code == 0, result.output
    assert "# Governance change proposal" in result.stdout
    assert "filesystem::read_file" in result.stdout

    # 2. JSON output
    result_json = runner.invoke(cli.app, ["policy", "propose", str(sample_gov_path), str(sample_observed_jsonl), "--json"])
    assert result_json.exit_code == 0, result_json.output
    parsed = json.loads(result_json.stdout)
    assert len(parsed["additions"]) == 1

    # 3. File output
    out_file = sample_gov_path.parent / "out.md"
    result_file = runner.invoke(cli.app, ["policy", "propose", str(sample_gov_path), str(sample_observed_jsonl), "-o", str(out_file)])
    assert result_file.exit_code == 0, result_file.output
    assert out_file.exists()
    assert "# Governance change proposal" in out_file.read_text()


def test_observation_window_schema_conformance(sample_observed_jsonl, artifact_format_checker):
    import jsonschema

    schema_path = Path(__file__).resolve().parent.parent / "contracts" / "observation-window.schema.json"
    schema = json.loads(schema_path.read_text())
    validator = jsonschema.Draft202012Validator(schema, format_checker=artifact_format_checker)

    for line in sample_observed_jsonl.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        validator.validate(record)



def test_propose_output_file_is_private_and_atomic(tmp_path, monkeypatch):
    import os
    import stat

    target = tmp_path / "proposal.md"
    monkeypatch.setattr(
        cli, "run_propose", lambda *a, **k: ({"proposed": []}, "# proposal\n")
    )

    result = CliRunner().invoke(
        cli.app,
        ["policy", "propose", "gov.yaml", "obs.jsonl", "--output", str(target)],
    )

    assert result.exit_code == 0, result.output
    assert target.read_text(encoding="utf-8") == "# proposal\n"
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["proposal.md"]


def test_propose_reports_malformed_governance_yaml_without_traceback(tmp_path):
    governance = tmp_path / "governance.yaml"
    governance.write_text("agents: [\n  - id: x\n bad")
    observed = tmp_path / "observed.jsonl"
    observed.write_text("")

    result = CliRunner().invoke(
        cli.app, ["policy", "propose", str(governance), str(observed)]
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert result.stderr.startswith("ERROR: ")
    assert result.stdout == ""


def test_load_export_refuses_an_oversized_file(tmp_path, monkeypatch):
    from toolgraph import policy_propose

    monkeypatch.setattr(policy_propose, "MAX_EXPORT_BYTES", 16)
    export = tmp_path / "observed.jsonl"
    export.write_text("x" * 17)

    with pytest.raises(policy_propose.PolicyProposeError) as raised:
        policy_propose.load_export(export)

    assert str(raised.value) == f"refusing to load {export}: exceeds the 16-byte limit"


def _seed_matching_graph(gov_path: Path) -> None:
    schema.init_schema()
    digest = governance_digest(load_governance(gov_path))
    with driver.session() as s:
        s.run("CREATE (t:Tool {key: 'filesystem::read_file', name: 'read_file'})")
        s.run("CREATE (t:Tool {key: 'filesystem::write_file', name: 'write_file'})")
        s.run(
            "MERGE (m:GraphMeta {id: 'singleton'}) "
            "SET m.instance_id = 'test-instance', m.generation = 1, m.governance_digest = $d",
            d=digest,
        )


@pytest.mark.parametrize("extra", [[], ["--json"]])
@pytest.mark.parametrize(
    ("field", "value", "position"),
    [
        ("tool_key", "filesystem::read_file?token=hunter2", "additions[0].tool"),
        ("tool_key", "filesystem::password=hunter2", "additions[0].tool"),
        ("candidate", "Authorization: Bearer hunter2", "insufficient_evidence[0].tool"),
    ],
)
def test_cli_propose_refuses_a_credential_shaped_ref(
    backend_graph, sample_gov_path, sample_observed_jsonl, extra, field, value, position
):
    _seed_matching_graph(sample_gov_path)
    lines = sample_observed_jsonl.read_text().splitlines()
    row = json.loads(lines[1])
    row.pop("tool_key")
    row[field] = value
    lines[1] = json.dumps(row)
    sample_observed_jsonl.write_text("\n".join(lines) + "\n")

    result = CliRunner().invoke(
        cli.app,
        ["policy", "propose", str(sample_gov_path), str(sample_observed_jsonl), *extra],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == (
        f"ERROR: refusing to emit a proposal: {position} "
        "contains credential-shaped data\n"
    )


@pytest.mark.parametrize(
    ("label", "shown"),
    [
        ("august token=hunter2", "august token=<redacted>"),
        ("august Bearer hunter2.x", "august Bearer <redacted>"),
    ],
)
def test_window_label_is_redacted_in_the_rendered_proposal(
    backend_graph, sample_gov_path, sample_observed_jsonl, label, shown
):
    _seed_matching_graph(sample_gov_path)

    _, rendered = run_propose(sample_gov_path, sample_observed_jsonl, window_label=label)

    [header] = [line for line in rendered.splitlines() if line.startswith("decision_mode:")]
    assert header.split(" · ")[2] == f"window: {shown}"
    assert "hunter2" not in rendered


def test_tool_key_with_credential_word_in_server_name_is_proposed(
    backend_graph, sample_gov_path, sample_observed_jsonl
):
    _seed_matching_graph(sample_gov_path)
    lines = sample_observed_jsonl.read_text().splitlines()
    row = json.loads(lines[1])
    row["tool_key"] = "vault-secret::read_file"
    lines[1] = json.dumps(row)
    sample_observed_jsonl.write_text("\n".join(lines) + "\n")

    proposal, _ = run_propose(sample_gov_path, sample_observed_jsonl)

    assert [row["tool"] for row in proposal["additions"]] == ["vault-secret::read_file"]


def test_output_identity_check_names_the_position_not_the_value():
    from toolgraph.policy_propose import ProvenanceError, _check_output_identities

    proposal = {
        "additions": [],
        "removals": [],
        "conflicts": [],
        "not_grant_shaped": [],
        "insufficient_evidence": [
            {
                "agent": "ops-agent",
                "tool": "read_file",
                "reason": "AMBIGUOUS_GRANT",
                "note": "matches a::read_file or https://u:pw@host/x::read_file",
            }
        ],
        "provenance": {"observed_state": None, "current_state": {"instance_id": "g1"}},
    }

    with pytest.raises(ProvenanceError) as raised:
        _check_output_identities(proposal)

    assert str(raised.value) == (
        "refusing to emit a proposal: insufficient_evidence[0].note "
        "contains credential-shaped data"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("srv::password=hunter2", True),
        ("Authorization: Bearer abc", True),
        ("bearer abc.def", True),
        ("api-key: xyz", True),
        ("srv::get_token", False),
        ("token::refresh", False),
        ("vault-secret::read_file", False),
        ("srv::secret:abc", True),
        ("filesystem::read_file", False),
        ("token bucket refill", False),
    ],
)
def test_contains_credential(text, expected):
    from toolgraph.redaction import contains_credential

    assert contains_credential(text) is expected

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
    with patch("toolgraph.graph.queries.with_graph_state", side_effect=RuntimeError("graph state changed during every read attempt")):
        with pytest.raises(StateChurnError, match="graph state churn during observation proposal"):
            run_propose(sample_gov_path, sample_observed_jsonl)


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


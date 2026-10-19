"""Tracegraph accepted-review to non-mutating policy work-plan tests."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from typer.testing import CliRunner

from tests._helpers import mock_policy_review_inputs
from toolgraph import cli
from toolgraph.policy_review import PolicyReviewPlanError, build_policy_review_plan

NOW = datetime(2026, 7, 14, tzinfo=UTC)
ROOT = Path(__file__).parent.parent


def test_review_plan_is_human_required_and_schema_valid(monkeypatch):
    mock_policy_review_inputs(monkeypatch)
    plan = build_policy_review_plan(report=Path("trace.json"), agent="planner", created_at=NOW)
    assert plan["decision_mode"] == "human_required"
    assert plan["graph_state"] == {"instance_id": "graph-1", "generation": 9}
    [candidate] = plan["candidates"]
    assert candidate["current_decision"] == "rejected"
    assert candidate["current_reason"] == "DENY_VIOLATION"
    assert candidate["automatic_change"] is False
    schema = json.loads((ROOT / "contracts" / "policy-review-plan.schema.json").read_text())
    Draft202012Validator(schema).validate(plan)


def test_review_plan_requires_an_accepted_candidate(monkeypatch):
    mock_policy_review_inputs(monkeypatch, accepted=False)
    with pytest.raises(PolicyReviewPlanError, match="no accepted"):
        build_policy_review_plan(report=Path("trace.json"), agent="planner")


def test_review_plan_cli_writes_private_atomic_artifact(monkeypatch, tmp_path):
    mock_policy_review_inputs(monkeypatch)
    monkeypatch.setattr(
        "toolgraph.policy_review.datetime",
        type("Frozen", (), {"now": staticmethod(lambda tz: NOW)}),
    )
    output = tmp_path / "policy-review-plan.json"
    result = CliRunner().invoke(
        cli.app,
        [
            "policy",
            "review-plan",
            "trace.json",
            "--agent",
            "planner",
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout)
    assert summary["decision_mode"] == "human_required"
    # Ecosystem-facing artifact digests are sha256:-prefixed (SyncMill's
    # ^sha256:[0-9a-f]{64}$); bare hex is reserved for bundle_digest.
    assert summary["artifact_digest"] == (
        "sha256:" + hashlib.sha256(output.read_bytes()).hexdigest()
    )
    assert json.loads(output.read_text())["candidates"][0]["automatic_change"] is False
    if os.name != "nt":
        assert output.stat().st_mode & 0o777 == 0o600


def test_review_plan_refuses_a_graph_without_instance_id(monkeypatch):
    mock_policy_review_inputs(monkeypatch)

    def without_instance(fn, *, strict):
        value = fn()
        value["graph_state"] = {"instance_id": None, "generation": 0}
        return value

    monkeypatch.setattr("toolgraph.policy_review.queries.with_graph_state", without_instance)

    with pytest.raises(PolicyReviewPlanError) as raised:
        build_policy_review_plan(report=Path("trace.json"), agent="planner")

    assert str(raised.value) == ("graph has no instance id — run init-schema before planning")


def test_policy_review_cli_scrubs_evidence(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from toolgraph import cli

    mock_policy_review_inputs(monkeypatch)
    monkeypatch.setattr(
        "toolgraph.policy_review.selector.eligible_tools",
        lambda *a, **k: {
            "agent_found": True,
            "eligible": [],
            "rejected": [
                {
                    "tool_key": "github::merge",
                    "reason": "DENY_VIOLATION",
                    "paths": [
                        {
                            "resource": "https://user:secret@host/r?token=secret#secret",
                            "provenance": {"evidence": "https://user:secret@host/r?token=secret"},
                            "exception_reason": "see https://host/r?token=secret",
                        }
                    ],
                }
            ],
        },
    )
    output = tmp_path / "review.json"
    result = CliRunner().invoke(
        cli.app,
        [
            "policy",
            "review-plan",
            "trace.json",
            "--agent",
            "planner",
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "secret" not in output.read_text()
    assert json.loads(output.read_text())["candidates"][0]["tool_key"] == "github::merge"


def test_policy_review_hides_quoted_userinfo_in_current_paths(monkeypatch):
    mock_policy_review_inputs(monkeypatch)
    monkeypatch.setattr(
        "toolgraph.policy_review.selector.eligible_tools",
        lambda *a, **k: {
            "agent_found": True,
            "eligible": [],
            "rejected": [
                {
                    "tool_key": "github::merge",
                    "reason": "DENY_VIOLATION",
                    "paths": [
                        '(github::merge) -WRITES-> (https://u:"hunter 2"@Host/r)',
                        "(github::merge) -READS-> (neo4j://u:%27p%20w%27@Host/DB?token=planted)",
                    ],
                }
            ],
        },
    )
    plan = build_policy_review_plan(report=Path("trace.json"), agent="planner")
    assert plan["candidates"][0]["current_paths"] == [
        "(github::merge) -WRITES-> (https://<redacted-uri>",
        "(github::merge) -READS-> (neo4j://<redacted-uri>",
    ]
    schema = json.loads((ROOT / "contracts" / "policy-review-plan.schema.json").read_text())
    Draft202012Validator(schema).validate(plan)
    assert "hunter" not in json.dumps(plan) and "planted" not in json.dumps(plan)

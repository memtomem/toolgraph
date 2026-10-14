"""Tracegraph accepted-review to non-mutating policy work-plan tests."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest
from typer.testing import CliRunner

from toolgraph import cli
from toolgraph.policy_review import PolicyReviewPlanError, build_policy_review_plan

NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)
ROOT = Path(__file__).parent.parent


def _accepted() -> dict:
    return {
        "schema_version": 1,
        "kind": "toolgraph.review-candidate-list",
        "source_report_digest": f"sha256:{'a' * 64}",
        "candidates": [
            {
                "candidate_id": "11111111-1111-5111-8111-111111111111",
                "run_id": "run-1",
                "pattern_id": "repeated-failure",
                "pattern_version": 1,
                "tool_key": "github::merge",
                "artifact_digest": f"sha256:{'b' * 64}",
                "disposition": "accepted",
                "history": [],
            }
        ],
    }


def _mock_inputs(monkeypatch, *, accepted: bool = True) -> None:
    payload = _accepted()
    if not accepted:
        payload["candidates"] = []
    monkeypatch.setattr("toolgraph.policy_review.list_candidates", lambda *a, **k: payload)
    monkeypatch.setattr(
        "toolgraph.policy_review.selector.eligible_tools",
        lambda *a, **k: {
            "agent": "planner",
            "agent_found": True,
            "profile": "strict",
            "eligible": [],
            "rejected": [
                {
                    "candidate": "github::merge",
                    "tool_key": "github::merge",
                    "reason": "DENY_VIOLATION",
                    "paths": ["(planner)-[:CAN_CALL]->(github::merge)-[:WRITES]->(prod)"],
                }
            ],
        },
    )

    def with_state(fn, *, strict):
        assert strict is True
        value = fn()
        value["graph_state"] = {"instance_id": "graph-1", "generation": 9}
        return value

    monkeypatch.setattr("toolgraph.policy_review.queries.with_graph_state", with_state)


def test_review_plan_is_human_required_and_schema_valid(monkeypatch):
    _mock_inputs(monkeypatch)
    plan = build_policy_review_plan(
        report=Path("trace.json"), agent="planner", created_at=NOW
    )
    assert plan["decision_mode"] == "human_required"
    assert plan["graph_state"] == {"instance_id": "graph-1", "generation": 9}
    [candidate] = plan["candidates"]
    assert candidate["current_decision"] == "rejected"
    assert candidate["current_reason"] == "DENY_VIOLATION"
    assert candidate["automatic_change"] is False
    schema = json.loads((ROOT / "contracts" / "policy-review-plan.schema.json").read_text())
    Draft202012Validator(schema).validate(plan)


def test_review_plan_requires_an_accepted_candidate(monkeypatch):
    _mock_inputs(monkeypatch, accepted=False)
    with pytest.raises(PolicyReviewPlanError, match="no accepted"):
        build_policy_review_plan(report=Path("trace.json"), agent="planner")


def test_review_plan_cli_writes_private_atomic_artifact(monkeypatch, tmp_path):
    _mock_inputs(monkeypatch)
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
    _mock_inputs(monkeypatch)

    def without_instance(fn, *, strict):
        value = fn()
        value["graph_state"] = {"instance_id": None, "generation": 0}
        return value

    monkeypatch.setattr(
        "toolgraph.policy_review.queries.with_graph_state", without_instance
    )

    with pytest.raises(PolicyReviewPlanError) as raised:
        build_policy_review_plan(report=Path("trace.json"), agent="planner")

    assert str(raised.value) == (
        "graph has no instance id — run init-schema before planning"
    )


def test_policy_review_cli_scrubs_evidence(monkeypatch, tmp_path):
    from tests.test_policy_review import _mock_inputs
    from toolgraph import cli
    from typer.testing import CliRunner

    _mock_inputs(monkeypatch)
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
                            "provenance": {
                                "evidence": "https://user:secret@host/r?token=secret"
                            },
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
    assert (
        json.loads(output.read_text())["candidates"][0]["tool_key"] == "github::merge"
    )

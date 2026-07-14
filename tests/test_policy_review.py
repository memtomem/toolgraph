"""Tracegraph accepted-review to non-mutating policy work-plan tests."""

from __future__ import annotations

from datetime import datetime, timezone
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
    assert json.loads(output.read_text())["candidates"][0]["automatic_change"] is False
    if os.name != "nt":
        assert output.stat().st_mode & 0o777 == 0o600

"""Portable policy-bundle producer and artifact tests."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest
from typer.testing import CliRunner

from toolgraph import cli
from toolgraph.artifacts import canonical_json_bytes, sha256_bytes
from toolgraph.graph import loader, queries
from toolgraph.manifest.ingest import ingest_governance
from toolgraph.models import AccessGrant, CrawlResult, Governance, ToolRecord
from toolgraph.policy_bundle import (
    PolicyBundleError,
    build_policy_bundle,
    tool_contract_digest,
)

NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)
ROOT = Path(__file__).parent.parent


def _mock_graph(
    monkeypatch, *, agent_found: bool = True, instance_id: str | None = "graph-instance-1"
) -> None:
    state = queries.GraphState(instance_id, 7, "a" * 64)
    contracts = [
        {
            "tool_key": "alpha::read",
            "server": "alpha",
            "name": "read",
            "description": "Read data",
            "input_schema": {"type": "object"},
            "read_only_hint": True,
            "destructive_hint": None,
            "idempotent_hint": None,
            "open_world_hint": None,
        },
        {
            "tool_key": "alpha::write",
            "server": "alpha",
            "name": "write",
            "description": "Write data",
            "input_schema": {"type": "object"},
            "read_only_hint": False,
            "destructive_hint": True,
            "idempotent_hint": None,
            "open_world_hint": None,
        },
    ]
    monkeypatch.setattr(queries, "graph_state", lambda: state)
    monkeypatch.setattr(queries, "exposed_tool_contracts", lambda: contracts)

    def filtered(*args, **kwargs):
        if not agent_found:
            return {
                "agent": "ghost",
                "agent_found": False,
                "profile": "strict",
                "eligible": [],
                "rejected": [],
            }
        return {
            "agent": "codex",
            "agent_found": True,
            "profile": "strict",
            "eligible": ["alpha::read"],
            "rejected": [
                {
                    "candidate": "alpha::write",
                    "tool_key": "alpha::write",
                    "reason": "DENY_VIOLATION",
                    "paths": [
                        "(alpha::write) -> (https://user:secret@example.test/data?q=token)"
                    ],
                }
            ],
        }

    monkeypatch.setattr("toolgraph.policy_bundle.selector.eligible_tools", filtered)
    monkeypatch.setattr(
        "toolgraph.policy_bundle.selector.rank_features",
        lambda *args, **kwargs: {
            "agent": "codex",
            "agent_found": agent_found,
            "features": [
                {"tool_key": "alpha::read", "candidate": "alpha::read", "risk_score": 0.0},
                {"tool_key": "alpha::write", "candidate": "alpha::write", "risk_score": 1.0},
            ] if agent_found else [],
        },
    )


def test_bundle_is_portable_deterministic_and_redacted(monkeypatch):
    _mock_graph(monkeypatch)
    first = build_policy_bundle(agent="codex", profile="strict", created_at=NOW)
    second = build_policy_bundle(agent="codex", profile="strict", created_at=NOW)
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert first["graph_state"] == {"instance_id": "graph-instance-1", "generation": 7}
    assert [row["tool_key"] for row in first["tools"]] == [
        "alpha::read",
        "alpha::write",
    ]
    assert first["tools"][0]["decision"] == "eligible"
    assert first["tools"][1]["reason"] == "DENY_VIOLATION"
    raw = canonical_json_bytes(first).decode()
    assert "secret" not in raw and "q=token" not in raw
    assert "https://example.test/data" in raw
    assert len(first["catalog_digest"]) == 64


def test_bundle_schema_accepts_producer(monkeypatch):
    _mock_graph(monkeypatch)
    schema = json.loads((ROOT / "contracts" / "policy-bundle.schema.json").read_text())
    Draft202012Validator(schema, format_checker=None).validate(
        build_policy_bundle(agent="codex", created_at=NOW)
    )


def test_golden_contract_and_bundle_bytes_pin_cross_repo_encoding():
    fixture = json.loads(
        (ROOT / "contracts" / "fixtures" / "tool-contract-v1.json").read_text()
    )
    assert tool_contract_digest(fixture["contract"]) == fixture["expected_digest"]

    bundle_path = ROOT / "contracts" / "fixtures" / "policy-bundle-v1.json"
    payload = bundle_path.read_bytes()
    bundle = json.loads(payload)
    assert canonical_json_bytes(bundle) == payload
    expected = (
        ROOT / "contracts" / "fixtures" / "policy-bundle-v1.sha256"
    ).read_text().strip()
    assert sha256_bytes(payload) == expected
    schema = json.loads((ROOT / "contracts" / "policy-bundle.schema.json").read_text())
    Draft202012Validator(schema, format_checker=None).validate(bundle)


def test_rejected_golden_bundle_pins_reason_paths_and_exact_bytes():
    bundle_path = ROOT / "contracts" / "fixtures" / "policy-bundle-v1-rejected.json"
    payload = bundle_path.read_bytes()
    bundle = json.loads(payload)
    assert canonical_json_bytes(bundle) == payload
    expected = (
        ROOT / "contracts" / "fixtures" / "policy-bundle-v1-rejected.sha256"
    ).read_text().strip()
    assert sha256_bytes(payload) == expected
    decision = bundle["tools"][0]
    assert decision["decision"] == "rejected"
    assert decision["reason"] == "DENY_VIOLATION"
    assert decision["paths"] == [
        "(fixture-agent)-[:CAN_CALL]->(도구-서버::publish)-[:WRITES]->"
        "(file:///demo/drafts)-[:GOVERNED_BY]->(draft-publish-deny)"
    ]
    schema = json.loads((ROOT / "contracts" / "policy-bundle.schema.json").read_text())
    Draft202012Validator(schema, format_checker=None).validate(bundle)


def test_unknown_agent_never_produces_bundle(monkeypatch):
    _mock_graph(monkeypatch, agent_found=False)
    with pytest.raises(PolicyBundleError, match="not found"):
        build_policy_bundle(agent="ghost", created_at=NOW)


def test_missing_graph_instance_id_never_produces_bundle(monkeypatch):
    _mock_graph(monkeypatch, instance_id=None)
    with pytest.raises(PolicyBundleError, match="no instance id"):
        build_policy_bundle(agent="codex", created_at=NOW)


def test_policy_compile_writes_private_exact_bytes(monkeypatch, tmp_path):
    _mock_graph(monkeypatch)
    monkeypatch.setattr(
        "toolgraph.policy_bundle.datetime",
        type("Frozen", (), {"now": staticmethod(lambda tz: NOW)}),
    )
    output = tmp_path / "nested" / "bundle.json"
    result = CliRunner().invoke(
        cli.app,
        ["policy", "compile", "--agent", "codex", "--output", str(output)],
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout)
    assert summary["bundle_digest"] == sha256_bytes(output.read_bytes())
    assert summary["eligible"] == 1 and summary["rejected"] == 1
    if os.name != "nt":
        assert output.stat().st_mode & 0o777 == 0o600


def test_compile_failure_preserves_existing_output(monkeypatch, tmp_path):
    output = tmp_path / "bundle.json"
    output.write_text("old\n")
    monkeypatch.setattr(
        cli,
        "build_policy_bundle",
        lambda **kwargs: (_ for _ in ()).throw(PolicyBundleError("stale")),
    )
    result = CliRunner().invoke(
        cli.app,
        ["policy", "compile", "--agent", "codex", "--output", str(output)],
    )
    assert result.exit_code == 1
    assert output.read_text() == "old\n"


def test_real_graph_bundle_carries_ingested_governance_digest(graph):
    loader.load_crawl_result(
        CrawlResult(
            server_name="alpha",
            tools=[ToolRecord(name="read"), ToolRecord(name="write")],
        )
    )
    report = ingest_governance(
        Governance(
            agents=["codex"],
            grants=[AccessGrant(agent="codex", tool="alpha::read")],
        )
    )
    assert report.applied
    bundle = build_policy_bundle(agent="codex", profile="strict", created_at=NOW)
    assert bundle["graph_state"]["instance_id"]
    assert bundle["governance_digest"] == queries.graph_state().governance_digest
    assert {row["tool_key"] for row in bundle["tools"]} == {
        "alpha::read",
        "alpha::write",
    }

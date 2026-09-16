"""Hermetic tests for bounded, body-free orchestration preflight."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from toolgraph import cli
from toolgraph.control_plan import (
    ControlPlanError,
    build_control_preflight,
    lint_control_plan,
    load_control_plan,
)


ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "contracts" / "fixtures" / "control-plan-v1.json"
NOW = datetime(2026, 8, 11, tzinfo=timezone.utc)


def _document() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _raw(document: dict | None = None) -> bytes:
    value = _document() if document is None else document
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _bracket(monkeypatch, *, rejected: bool = False) -> None:
    calls = []

    def eligible(principal, candidates, *, profile):
        calls.append((principal, candidates, profile))
        rejected_rows = []
        eligible_rows = list(candidates)
        if rejected and principal == "claude":
            eligible_rows = candidates[:-1]
            rejected_rows = [
                {
                    "candidate": candidates[-1],
                    "tool_key": candidates[-1],
                    "reason": "DENY_VIOLATION",
                    "paths": [],
                }
            ]
        return {
            "agent": principal,
            "agent_found": True,
            "profile": profile,
            "eligible": eligible_rows,
            "rejected": rejected_rows,
        }

    def bracket(fetch, *, strict):
        assert strict is True
        return {
            **fetch(),
            "graph_state": {"instance_id": "test-instance", "generation": 42},
        }

    class MockSession:
        def run(self, *args, **kwargs):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr("toolgraph.control_plan.selector.eligible_tools", eligible)
    monkeypatch.setattr("toolgraph.control_plan.queries.with_graph_state", bracket)
    monkeypatch.setattr("toolgraph.control_plan.driver.session", lambda: MockSession())
    monkeypatch.setattr("toolgraph.control_plan._test_calls", calls, raising=False)


def test_valid_plan_is_body_free_and_structurally_clean():
    plan = load_control_plan(_raw())
    assert plan.run_id == "run-control-1"
    assert lint_control_plan(plan) == []


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("producer", "prompt"), "do the work"),
        (("producer", "system_prompt"), "do the work"),
        (("producer", "api_key_hint"), "placeholder"),
        (("producer", "repository_path"), "relative/is-still-a-body-field"),
        (("producer", "note"), "/private/workspace"),
        (("producer", "note"), "C:\\Users\\operator\\workspace"),
        (("producer", "note"), "\\\\server\\share\\workspace"),
        (("producer", "note"), "https://user:secret@example.test/resource"),
        (("producer", "note"), "https://example.test/resource?token=secret"),
    ],
)
def test_body_free_scanner_rejects_sensitive_shape(path, value):
    document = _document()
    parent = document
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    with pytest.raises(ControlPlanError):
        load_control_plan(_raw(document))


def test_same_major_additive_fields_are_tolerated():
    document = _document()
    document["future_metadata"] = {"bounded_hint": 3}
    document["nodes"][1]["future_node_hint"] = True
    assert load_control_plan(_raw(document)).run_id == "run-control-1"


def test_unknown_major_is_rejected():
    document = _document()
    document["schema_version"] = 2
    with pytest.raises(ControlPlanError, match="invalid control plan"):
        load_control_plan(_raw(document))


def test_duplicate_json_keys_are_rejected_before_model_validation():
    raw = _raw().replace(
        b'"schema_version":1', b'"schema_version":2,"schema_version":1', 1
    )
    with pytest.raises(ControlPlanError, match="duplicate key"):
        load_control_plan(raw)


def test_agent_kind_and_call_flag_must_agree():
    document = _document()
    document["nodes"][1]["agent_call"] = False
    for key in ("max_invocations", "max_parallelism", "timeout_seconds"):
        document["nodes"][1].pop(key)
    with pytest.raises(ControlPlanError, match="must agree"):
        load_control_plan(_raw(document))


def test_linter_reports_cycles_unknown_edges_and_low_bounds():
    document = _document()
    document["edges"].append(
        {
            "from": "end",
            "to": "attempts",
            "kind": "fanout",
            "decided_by": "model",
        }
    )
    document["edges"].append(
        {"from": "missing", "to": "end", "kind": "always", "decided_by": "code"}
    )
    document["limits"]["maximum_agent_calls"] = 1
    document["limits"]["maximum_parallelism"] = 1
    codes = {row["code"] for row in lint_control_plan(load_control_plan(_raw(document)))}
    assert {
        "UNKNOWN_NODE_REFERENCE",
        "CONTROL_CYCLE_UNSUPPORTED",
        "MODEL_FANOUT_UNBOUNDED",
        "CALL_BOUND_TOO_LOW",
        "PARALLEL_BOUND_TOO_LOW",
    } <= codes


def test_build_preflight_brackets_every_principal_in_one_graph_state(monkeypatch):
    _bracket(monkeypatch)
    raw = _raw()
    artifact = build_control_preflight(raw, profile="review", created_at=NOW)
    assert artifact["decision"] == "advisory_allow"
    assert artifact["created_at"] == "2026-08-11T00:00:00+00:00"
    assert artifact["plan_digest"] == f"sha256:{hashlib.sha256(raw).hexdigest()}"
    assert artifact["graph_state"] == {"instance_id": "test-instance", "generation": 42}
    assert [row["principal"] for row in artifact["evaluations"]] == ["codex", "claude"]


def test_build_preflight_warns_without_becoming_an_enforcer(monkeypatch):
    _bracket(monkeypatch, rejected=True)
    artifact = build_control_preflight(_raw(), profile="strict", created_at=NOW)
    assert artifact["mode"] == "advisory"
    assert artifact["decision"] == "advisory_warn"
    assert artifact["evaluations"][1]["rejected"]


def test_missing_graph_instance_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "toolgraph.control_plan.queries.with_graph_state",
        lambda fetch, **kwargs: {**fetch(), "graph_state": {"instance_id": "", "generation": 1}},
    )
    monkeypatch.setattr(
        "toolgraph.control_plan.selector.eligible_tools",
        lambda principal, candidates, **kwargs: {
            "agent_found": True,
            "eligible": candidates,
            "rejected": [],
        },
    )
    class MockSession:
        def run(self, *args, **kwargs):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr("toolgraph.control_plan.driver.session", lambda: MockSession())
    with pytest.raises(ControlPlanError, match="no instance id"):
        build_control_preflight(_raw())


def test_cli_writes_private_canonical_artifact(monkeypatch, tmp_path):
    """Drive the real producer: a stub here cannot catch producer/contract drift."""
    _bracket(monkeypatch)
    plan = tmp_path / "plan.json"
    plan.write_bytes(_raw())
    out = tmp_path / "nested" / "result.json"
    result = CliRunner().invoke(
        cli.app,
        ["control-preflight", str(plan), "--profile", "review", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    written = json.loads(out.read_text())
    assert written["kind"] == "toolgraph.control-preflight"
    assert written["run_id"] == "run-control-1"
    if os.name != "nt":
        assert out.stat().st_mode & 0o777 == 0o600
    assert out.read_bytes().endswith(b"\n")

    envelope = json.loads(result.stdout)
    assert envelope["artifact_digest"] == (
        "sha256:" + hashlib.sha256(out.read_bytes()).hexdigest()
    )
    assert envelope["plan_digest"] == written["plan_digest"]
    assert envelope["run_id"] == written["run_id"]
    assert envelope["graph_state"] == written["graph_state"]
    assert envelope["decision"] == written["decision"]
    assert envelope["evaluations"] == len(written["evaluations"])


def test_naive_created_at_is_rejected_before_stamping(monkeypatch):
    """RFC 3339 needs an offset; policy compile already guards this."""
    _bracket(monkeypatch)
    with pytest.raises(ControlPlanError, match="timezone-aware"):
        build_control_preflight(_raw(), created_at=datetime(2026, 8, 11))


def test_uri_shaped_identities_survive_redaction(monkeypatch):
    """Redaction is for graph evidence; mangling identity breaks correlation."""
    _bracket(monkeypatch)
    document = _document()
    document["run_id"] = "https://runs.example/r?id=42"
    artifact = build_control_preflight(_raw(document), profile="review")
    assert artifact["run_id"] == "https://runs.example/r?id=42"


def test_cli_failure_preserves_existing_output(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli,
        "build_control_preflight",
        lambda *args, **kwargs: (_ for _ in ()).throw(ControlPlanError("bad plan")),
    )
    plan = tmp_path / "plan.json"
    plan.write_bytes(_raw())
    out = tmp_path / "result.json"
    out.write_text("keep", encoding="utf-8")
    result = CliRunner().invoke(
        cli.app, ["control-preflight", str(plan), "--out", str(out)]
    )
    assert result.exit_code == 1
    assert out.read_text(encoding="utf-8") == "keep"


def test_deeply_nested_plan_fails_typed_not_recursion_error():
    import json as _json

    from toolgraph.control_plan import ControlPlanError, load_control_plan

    nested: dict = {"x": 1}
    for _ in range(150):
        nested = {"n": nested}
    raw = _json.dumps({"schema_version": 1, "deep": nested}).encode()
    with pytest.raises(ControlPlanError, match="nesting exceeds"):
        load_control_plan(raw)


def test_control_plan_transitive_exposure_and_redaction(monkeypatch, artifact_format_checker):
    import jsonschema

    class MockExposureSession:
        def run(self, query, keys=None, **kwargs):
            # mock tool data access
            rows = []
            if "filesystem::read_file" in (keys or []):
                rows.append({
                    "tool_key": "filesystem::read_file",
                    "mode": "READS",
                    "resource_uri": "https://api.test/data?token=supersecret123",
                })
            if "filesystem::write_file" in (keys or []):
                rows.append({
                    "tool_key": "filesystem::write_file",
                    "mode": "WRITES",
                    "resource_uri": "https://user:password123@storage.test/vault",
                })
            return rows

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def eligible(principal, candidates, *, profile):
        return {
            "agent": principal,
            "agent_found": True,
            "profile": profile,
            "eligible": list(candidates),
            "rejected": [],
        }

    def bracket(fetch, *, strict):
        assert strict is True
        return {
            **fetch(),
            "graph_state": {"instance_id": "test-instance", "generation": 42},
        }

    monkeypatch.setattr("toolgraph.control_plan.selector.eligible_tools", eligible)
    monkeypatch.setattr("toolgraph.control_plan.queries.with_graph_state", bracket)
    monkeypatch.setattr("toolgraph.control_plan.driver.session", lambda: MockExposureSession())

    # Build plan where start -> reader_node -> router -> writer_node -> end
    doc = {
        "schema_version": 1,
        "kind": "toolgraph.control-plan",
        "run_id": "exposure-run-1",
        "producer": {
            "name": "test-orch",
            "version": "1.0",
        },
        "limits": {
            "maximum_agent_calls": 10,
            "maximum_parallelism": 2,
            "maximum_cycles": 0,
        },
        "principals": [
            {"id": "p_reader", "candidate_tools": ["filesystem::read_file"]},
            {"id": "p_writer", "candidate_tools": ["filesystem::write_file"]},
        ],
        "nodes": [
            {"id": "start", "kind": "start", "principals": [], "agent_call": False},
            {
                "id": "node_reader",
                "kind": "agent",
                "agent_call": True,
                "principals": ["p_reader"],
                "max_invocations": 1,
                "max_parallelism": 1,
                "timeout_seconds": 60,
            },
            {"id": "router", "kind": "join", "principals": [], "agent_call": False},
            {
                "id": "node_writer",
                "kind": "agent",
                "agent_call": True,
                "principals": ["p_writer"],
                "max_invocations": 1,
                "max_parallelism": 1,
                "timeout_seconds": 60,
            },
            {"id": "end", "kind": "end", "principals": [], "agent_call": False},
        ],
        "edges": [
            {"from": "start", "to": "node_reader", "kind": "always", "decided_by": "code"},
            {"from": "node_reader", "to": "router", "kind": "always", "decided_by": "code"},
            {"from": "router", "to": "node_writer", "kind": "always", "decided_by": "code"},
            {"from": "node_writer", "to": "end", "kind": "always", "decided_by": "code"},
        ],
    }

    raw = json.dumps(doc).encode()
    res = build_control_preflight(raw, profile="strict")

    # 1. Decision must be advisory_warn due to exposure
    assert res["decision"] == "advisory_warn"

    # 2. Potential exposures detected
    exposures = res.get("potential_exposures", [])
    assert len(exposures) == 1
    exp = exposures[0]
    assert exp["reader_node"] == "node_reader"
    assert exp["writer_node"] == "node_writer"
    assert exp["witness_path"] == ["node_reader", "router", "node_writer"]

    # 3. Credential scrubbing: neither supersecret123 nor password123 in serialized output!
    serialized = json.dumps(res)
    assert "supersecret123" not in serialized
    assert "password123" not in serialized
    assert "<redacted:credential>" in exp["read_resource"] or "token=" not in exp["read_resource"]
    assert "<redacted:credential>" in exp["write_resource"] or "password" not in exp["write_resource"]

    # 4. v1 schema validation
    schema_path = ROOT / "contracts" / "control-preflight.schema.json"
    schema = json.loads(schema_path.read_text())
    validator = jsonschema.Draft202012Validator(schema, format_checker=artifact_format_checker)
    validator.validate(res)


def test_control_plan_cycle_and_invalid_edge_safe_traversal(monkeypatch):
    class MockExposureSession:
        def run(self, query, keys=None, **kwargs):
            return [
                {"tool_key": "t::read", "mode": "READS", "resource_uri": "res://a"},
                {"tool_key": "t::write", "mode": "WRITES", "resource_uri": "res://b"},
            ]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr("toolgraph.control_plan.selector.eligible_tools", lambda p, c, **kwargs: {
        "agent": p, "agent_found": True, "profile": kwargs.get("profile", "strict"), "eligible": list(c), "rejected": []
    })
    monkeypatch.setattr("toolgraph.control_plan.queries.with_graph_state", lambda fetch, **kwargs: {
        **fetch(), "graph_state": {"instance_id": "test-instance", "generation": 1}
    })
    monkeypatch.setattr("toolgraph.control_plan.driver.session", lambda: MockExposureSession())

    doc = {
        "schema_version": 1,
        "kind": "toolgraph.control-plan",
        "run_id": "cycle-run",
        "producer": {"name": "test-orch", "version": "1.0"},
        "limits": {"maximum_agent_calls": 5, "maximum_parallelism": 1, "maximum_cycles": 0},
        "principals": [
            {"id": "p1", "candidate_tools": ["t::read", "t::write"]},
        ],
        "nodes": [
            {"id": "start", "kind": "start", "principals": [], "agent_call": False},
            {
                "id": "n1",
                "kind": "agent",
                "agent_call": True,
                "principals": ["p1"],
                "max_invocations": 1,
                "max_parallelism": 1,
                "timeout_seconds": 60,
            },
            {
                "id": "n2",
                "kind": "agent",
                "agent_call": True,
                "principals": ["p1"],
                "max_invocations": 1,
                "max_parallelism": 1,
                "timeout_seconds": 60,
            },
            {"id": "end", "kind": "end", "principals": [], "agent_call": False},
        ],
        "edges": [
            {"from": "start", "to": "n1", "kind": "always", "decided_by": "code"},
            {"from": "n1", "to": "n2", "kind": "always", "decided_by": "code"},
            # Cycle n2 -> n1: must NOT loop forever
            {"from": "n2", "to": "n1", "kind": "always", "decided_by": "code"},
            # Invalid edge to non-existent node: must be skipped safely
            {"from": "n2", "to": "ghost_node", "kind": "always", "decided_by": "code"},
            {"from": "n2", "to": "end", "kind": "always", "decided_by": "code"},
        ],
    }

    res = build_control_preflight(json.dumps(doc).encode())
    assert res["decision"] == "advisory_warn"
    assert len(res["potential_exposures"]) >= 1


def test_control_plan_4096_chain_and_budget_truncation(monkeypatch):
    class MockExposureSession:
        def run(self, query, keys=None, **kwargs):
            return [
                {"tool_key": "t::read", "mode": "READS", "resource_uri": "res://r"},
                {"tool_key": "t::write", "mode": "WRITES", "resource_uri": "res://w"},
            ]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr("toolgraph.control_plan.selector.eligible_tools", lambda p, c, **kwargs: {
        "agent": p, "agent_found": True, "profile": kwargs.get("profile", "strict"), "eligible": list(c), "rejected": []
    })
    monkeypatch.setattr("toolgraph.control_plan.queries.with_graph_state", lambda fetch, **kwargs: {
        **fetch(), "graph_state": {"instance_id": "test-instance", "generation": 1}
    })
    monkeypatch.setattr("toolgraph.control_plan.driver.session", lambda: MockExposureSession())

    # Build 100 node chain: start -> n_0 -> ... -> n_99 -> end
    nodes = [{"id": "start", "kind": "start", "principals": [], "agent_call": False}]
    edges = []
    prev = "start"
    for i in range(100):  # 100 intermediate nodes for test speed
        nid = f"n_{i}"
        principals = ["p_reader"] if i == 0 else (["p_writer"] if i == 99 else [])
        agent_call = bool(principals)
        node_def = {
            "id": nid,
            "kind": "agent" if agent_call else "join",
            "principals": principals,
            "agent_call": agent_call,
        }
        if agent_call:
            node_def["max_invocations"] = 1
            node_def["max_parallelism"] = 1
            node_def["timeout_seconds"] = 60
        nodes.append(node_def)
        edges.append({"from": prev, "to": nid, "kind": "always", "decided_by": "code"})
        prev = nid
    nodes.append({"id": "end", "kind": "end", "principals": [], "agent_call": False})
    edges.append({"from": prev, "to": "end", "kind": "always", "decided_by": "code"})

    doc = {
        "schema_version": 1,
        "kind": "toolgraph.control-plan",
        "run_id": "chain-run",
        "producer": {"name": "test-orch", "version": "1.0"},
        "limits": {"maximum_agent_calls": 10, "maximum_parallelism": 1, "maximum_cycles": 0},
        "principals": [
            {"id": "p_reader", "candidate_tools": ["t::read"]},
            {"id": "p_writer", "candidate_tools": ["t::write"]},
        ],
        "nodes": nodes,
        "edges": edges,
    }

    res = build_control_preflight(json.dumps(doc).encode())
    assert res["decision"] == "advisory_warn"
    assert len(res["potential_exposures"]) == 1
    exp = res["potential_exposures"][0]
    witness = exp["witness_path"]
    # The 32-element budget includes the omitted-nodes marker
    assert len(witness) == 32
    assert "... (69 intermediate nodes) ..." in witness[16]


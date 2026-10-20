"""Control-plan identities get the identity rule, and no error carries one.

Every string field of a v1 plan that names something (the run, the producer,
principals, nodes, edge endpoints, candidate tools) is checked with
`is_credential_shaped_identity`; so is every key, since a key is a field
name. String values of additive fields are prose and keep the prose rule.
"""

from __future__ import annotations

import copy
import json
import traceback

import pytest
from typer.testing import CliRunner

from tests._helpers import control_plan_bracket, control_plan_document, control_plan_raw
from toolgraph import cli, control_plan
from toolgraph.control_plan import ControlPlanError, build_control_preflight, load_control_plan

# The prose rule accepts both (a '/' in the password; a percent-encoded scheme).
PLANTED = ["neo4j://u:QX/ZQ@host/r", "neo4j%3A%2F%2Fu%3AQX/ZQ@host/r"]


def _rename_principal(plan: dict, value: str) -> None:
    old = plan["principals"][0]["id"]
    plan["principals"][0]["id"] = value
    for node in plan["nodes"]:
        node["principals"] = [value if p == old else p for p in node["principals"]]


def _rename_node(plan: dict, value: str) -> None:
    old = plan["nodes"][1]["id"]
    plan["nodes"][1]["id"] = value
    for edge in plan["edges"]:
        for end in ("from", "to"):
            if edge[end] == old:
                edge[end] = value


FIELDS = {
    "run_id": (lambda p, v: p.__setitem__("run_id", v), "run_id"),
    "producer.name": (lambda p, v: p["producer"].__setitem__("name", v), "producer"),
    "producer.version": (lambda p, v: p["producer"].__setitem__("version", v), "producer"),
    "principal id": (_rename_principal, "principals.0"),
    "node principal": (
        lambda p, v: p["nodes"][1]["principals"].append(v),
        "nodes.1",
    ),
    "node id": (_rename_node, "nodes.1"),
    "edge from": (lambda p, v: p["edges"][0].__setitem__("from", v), "edges.0"),
    "edge to": (lambda p, v: p["edges"][0].__setitem__("to", v), "edges.0"),
    "candidate tool": (
        lambda p, v: p["principals"][0]["candidate_tools"].append(f"srv::{v}"),
        "principals.0",
    ),
}


def _plan_with(field: str, value: str) -> bytes:
    plan = copy.deepcopy(control_plan_document())
    FIELDS[field][0](plan, value)
    return control_plan_raw(plan)


def _assert_hidden(text: str) -> None:
    assert "QX" not in text
    assert "ZQ" not in text


def _formatted(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (field, value)
        for field in FIELDS
        for value in PLANTED
        # A raw ':' already fails the qualified-key pattern, as on main.
        if not (field == "candidate tool" and value == PLANTED[0])
    ],
)
def test_plan_identities_refuse_credential_shapes(field, value):
    raw = _plan_with(field, value)
    assert value in raw.decode()

    with pytest.raises(ControlPlanError) as raised:
        load_control_plan(raw)

    message = str(raised.value)
    assert message.startswith(f"invalid control plan at {FIELDS[field][1]}")
    assert "credential-shaped" in message
    _assert_hidden(message)
    _assert_hidden(_formatted(raised.value))


def test_additive_prose_keeps_the_prose_rule():
    plan = copy.deepcopy(control_plan_document())
    plan["producer"]["note"] = "see https://docs.example and mail ops@example.com"

    assert load_control_plan(control_plan_raw(plan)).run_id == "run-control-1"


@pytest.mark.parametrize("key", PLANTED)
def test_a_credential_shaped_key_is_refused_before_it_can_appear_in_a_path(key):
    plan = copy.deepcopy(control_plan_document())
    plan["producer"][key] = {"body": "bounded"}

    with pytest.raises(ControlPlanError) as raised:
        load_control_plan(control_plan_raw(plan))

    assert str(raised.value) == "control plan contains a credential-shaped key in producer"
    _assert_hidden(_formatted(raised.value))


@pytest.mark.parametrize("key", PLANTED)
def test_a_duplicate_credential_shaped_key_is_not_echoed(key):
    raw = control_plan_raw().replace(
        b'"producer":{', f'"producer":{{"{key}":1,"{key}":2,'.encode(), 1
    )
    assert key.encode() in raw

    with pytest.raises(ControlPlanError) as raised:
        load_control_plan(raw)

    assert str(raised.value) == "control plan contains duplicate key a credential-shaped key"


def test_graph_state_goes_through_artifact_safety(monkeypatch):
    control_plan_bracket(monkeypatch)
    real = control_plan.queries.with_graph_state

    def planted(fetch, *, strict):
        return {**real(fetch, strict=strict), "graph_state": {"instance_id": PLANTED[0]}}

    monkeypatch.setattr("toolgraph.control_plan.queries.with_graph_state", planted)

    with pytest.raises(ValueError) as raised:
        build_control_preflight(control_plan_raw())

    assert str(raised.value) == "artifact identity contains credential-shaped data"


def test_cli_refuses_without_echoing(monkeypatch, tmp_path):
    control_plan_bracket(monkeypatch)
    plan = tmp_path / "plan.json"
    plan.write_bytes(_plan_with("run_id", PLANTED[0]))

    result = CliRunner().invoke(
        cli.app, ["control-preflight", str(plan), "--out", str(tmp_path / "r.json")]
    )

    assert result.exit_code == 1
    assert "invalid control plan at run_id" in result.output
    _assert_hidden(result.output)
    assert not (tmp_path / "r.json").exists()


def test_syncmill_shaped_identities_still_pass(monkeypatch):
    control_plan_bracket(monkeypatch)
    plan = copy.deepcopy(control_plan_document())
    _rename_principal(plan, "unmapped-0a1b2c3d4e5f")
    plan["run_id"] = "syncmill-run-2026-10-20T09:00:00Z"

    artifact = build_control_preflight(control_plan_raw(plan))

    assert artifact["run_id"] == plan["run_id"]
    assert json.dumps(artifact).count("unmapped-0a1b2c3d4e5f") >= 1

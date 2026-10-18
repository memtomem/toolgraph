"""Every value Toolgraph refuses rather than scrubs goes through one rule.

`redaction.is_credential_shaped` is that rule. Each surface below embeds the
value in a payload that is otherwise valid for its field, so a refusal comes
from the credential rule and not from the field's own grammar, and asserts
the error names credentials without echoing the value.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from tests._helpers import control_plan_document, control_plan_raw
from toolgraph.artifact_safety import safe_artifact, validate_identity
from toolgraph.control_plan import ControlPlanError, load_control_plan
from toolgraph.policy_impact_models import clean_identity
from toolgraph.policy_propose import ProvenanceError, _instance_id, _load_rows, _refuse_credential
from toolgraph.redaction import is_credential_shaped
from toolgraph.review_candidates import (
    ReviewCandidateError,
    annotate_candidate,
    default_annotations_path,
    load_report,
    normalize_note,
    normalize_reviewer,
)

ROOT = Path(__file__).parents[1]
GOLDEN = ROOT / "contracts" / "tracegraph" / "v1.json"
CANDIDATE_ID = "167379e8-3436-546b-9dbb-5fa087ab0493"

# Credential shapes without whitespace, valid in every field grammar below.
REFUSED_COMPACT = [
    "password=hunter2",
    "Authorization=abc123",
    "mycredential=x",
    '"token"=abc',
    '"credential":"abc"',
    '"token":123456',
    "https://u:pw@host/x",
    "https://host/p?token=abc",
    "https://host/p?%74oken=abc",
    "https://host/p?access%5Ftoken=abc",
    "https://host/p?%63redential=abc",
]
# Shapes that need whitespace, for fields that allow it.
REFUSED_SPACED = [
    "Bearer abc123",
    "Authorization: Bearer abc123",
    "api_key: x",
    "token: rotated weekly",
    "Authorization:Basic dXNlcjpwYXNz",
    "Authorization:Token abc123",
]
# Names a tool, agent or note can legitimately carry.
ACCEPTED_COMPACT = [
    "get_token:v2",
    "agent-token:blue",
    "authorization:v2",
    "secretary=bob",
    "password-reset",
    "Bearer+abc123",
    # Decoded once only: a doubly encoded key is a documented boundary.
    "https://host/p?%2574oken=abc",
]
ACCEPTED_SPACED = ["rotate the password weekly", "token bucket refill", "see the server log"]


def _review_candidate(field, tmp_path_factory):
    def check(value):
        report = json.loads(GOLDEN.read_text())
        report["candidates"][0][field] = f"srv::{value}" if field == "tool_key" else value
        path = tmp_path_factory.mktemp("report") / "candidates.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        load_report(path)

    return check


def _control_plan_value(value):
    document = control_plan_document()
    document["producer"]["note"] = value
    load_control_plan(control_plan_raw(document))


def _control_plan_key(value):
    document = control_plan_document()
    document["producer"][value] = "bounded"
    load_control_plan(control_plan_raw(document))


def _graph_instance_id(value):
    safe_artifact({"graph_instance_id": value})


def _graph_state_instance_id(value):
    safe_artifact({"graph_state": {"instance_id": value}})


# name -> (check, field allows whitespace, legitimate values can reach the rule).
# A control-plan key holding `token` or `password` at all is already refused by
# the body-field blacklist, so legitimate names never reach the rule there.
SURFACES = {
    "artifact identity": (lambda _: validate_identity, True, True),
    "artifact graph_instance_id": (lambda _: _graph_instance_id, True, True),
    "artifact graph_state.instance_id": (lambda _: _graph_state_instance_id, True, True),
    "policy impact identity": (lambda _: clean_identity, True, True),
    "policy propose identity": (lambda _: _refuse_credential, True, True),
    "control plan string": (lambda _: _control_plan_value, True, True),
    "control plan key": (lambda _: _control_plan_key, True, False),
    "review note": (lambda _: normalize_note, True, True),
    "review reviewer": (lambda _: normalize_reviewer, True, True),
    "review candidate run_id": (lambda f: _review_candidate("run_id", f), False, True),
    "review candidate tool_key": (lambda f: _review_candidate("tool_key", f), False, True),
}


def _cases(compact, spaced, *, accepted):
    return [
        pytest.param(make, value, id=f"{name}:{value}")
        for name, (make, allows_spaces, reachable) in SURFACES.items()
        if reachable or not accepted
        for value in compact + (spaced if allows_spaces else [])
    ]


@pytest.mark.parametrize(("make", "value"), _cases(REFUSED_COMPACT, REFUSED_SPACED, accepted=False))
def test_credential_shapes_are_refused_without_echoing_them(make, value, tmp_path_factory):
    with pytest.raises((ValueError, ControlPlanError, ReviewCandidateError)) as raised:
        make(tmp_path_factory)(value)

    message = str(raised.value)
    assert "credential" in message
    assert value not in message


@pytest.mark.parametrize(
    ("make", "value"), _cases(ACCEPTED_COMPACT, ACCEPTED_SPACED, accepted=True)
)
def test_legitimate_names_and_prose_pass(make, value, tmp_path_factory):
    make(tmp_path_factory)(value)


@pytest.mark.parametrize(
    "value", ["oauth::get_token:v2", "token::refresh", "vault-secret::read_file"]
)
def test_server_qualified_tool_keys_pass(value):
    assert not is_credential_shaped(value)


def test_duplicate_credential_shaped_key_is_not_echoed():
    raw = control_plan_raw(control_plan_document()).replace(
        b'"producer":{', b'"producer":{"Bearer abc123":1,"Bearer abc123":2,', 1
    )
    assert b"Bearer abc123" in raw

    with pytest.raises(ControlPlanError) as raised:
        load_control_plan(raw)

    assert str(raised.value) == "control plan contains duplicate key a credential-shaped key"


def test_refused_review_note_writes_nothing(tmp_path):
    source = tmp_path / "candidates.json"
    source.write_bytes(GOLDEN.read_bytes())

    with pytest.raises(ReviewCandidateError):
        annotate_candidate(
            source,
            CANDIDATE_ID,
            disposition="accepted",
            reviewer="operator",
            note="token: abc123",
        )

    assert not default_annotations_path(source).exists()


def test_one_rule_serves_every_refusal_surface():
    """No module outside redaction.py reaches for the old detectors.

    Structural, not a substring match: it inspects imports from and attribute
    reads of `toolgraph.redaction`. It cannot prove the shared rule is called,
    only that no surface grows a detector of its own from these parts.
    """
    detectors = {"CREDENTIAL_URI", "CREDENTIAL_QUERY", "CREDENTIAL_KEYWORDS", "contains_credential"}
    offenders = []
    for path in sorted((ROOT / "toolgraph").rglob("*.py")):
        if path.name == "redaction.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module == "toolgraph.redaction":
                names = {alias.name for alias in node.names}
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                names = {node.attr} if node.value.id == "redaction" else set()
            else:
                continue
            offenders += [f"{path.relative_to(ROOT)}: {name}" for name in names & detectors]
    assert offenders == []


def test_export_rows_are_refused_before_a_duplicate_error_can_quote_them(tmp_path):
    row = json.dumps({"agent": "bot", "decision": "allowed", "tool_key": "srv::t?%74oken=SECRET"})
    lines = [(1, row), (2, row)]

    with pytest.raises(ValueError) as raised:
        _load_rows(tmp_path / "observed.jsonl", lines)

    assert "tool_key contains credential-shaped data" in str(raised.value)
    assert "SECRET" not in str(raised.value)


def test_envelope_instance_id_is_refused_before_a_mismatch_error_can_quote_it():
    with pytest.raises(ProvenanceError) as raised:
        _instance_id("graph?%74oken=SECRET", "graph_instance_id")

    assert str(raised.value) == "graph_instance_id contains credential-shaped data"


def test_decoding_inspects_but_never_rewrites_an_identity():
    value = "agent%20one"
    assert not is_credential_shaped(value)
    assert safe_artifact({"agent": value}) == {"agent": value}

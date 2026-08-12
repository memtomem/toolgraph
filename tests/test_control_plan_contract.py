"""JSON-schema contract tests for control plan exchange artifacts."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest


ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "contracts"
FIXTURES = CONTRACTS / "fixtures"


@pytest.mark.parametrize(
    ("schema_name", "fixture_name"),
    [
        ("control-plan.schema.json", "control-plan-v1.json"),
        ("control-preflight.schema.json", "control-preflight-v1.json"),
    ],
)
def test_golden_fixture_validates(schema_name, fixture_name):
    schema = json.loads((CONTRACTS / schema_name).read_text(encoding="utf-8"))
    fixture = json.loads((FIXTURES / fixture_name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(fixture)


def test_input_schema_allows_additive_fields_but_rejects_new_major():
    schema = json.loads((CONTRACTS / "control-plan.schema.json").read_text())
    fixture = json.loads((FIXTURES / "control-plan-v1.json").read_text())
    additive = deepcopy(fixture)
    additive["future_metadata"] = {"bounded_hint": 2}
    Draft202012Validator(schema).validate(additive)
    additive["schema_version"] = 2
    errors = list(Draft202012Validator(schema).iter_errors(additive))
    assert any(list(error.absolute_path) == ["schema_version"] for error in errors)


def test_output_graph_state_is_closed():
    schema = json.loads((CONTRACTS / "control-preflight.schema.json").read_text())
    fixture = json.loads((FIXTURES / "control-preflight-v1.json").read_text())
    fixture["graph_state"]["unexpected"] = True
    with pytest.raises(Exception):
        Draft202012Validator(schema).validate(fixture)


def test_golden_result_binds_the_exact_golden_plan_bytes():
    raw_plan = (FIXTURES / "control-plan-v1.json").read_bytes()
    result = json.loads((FIXTURES / "control-preflight-v1.json").read_text())
    assert result["plan_digest"] == f"sha256:{hashlib.sha256(raw_plan).hexdigest()}"

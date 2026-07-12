"""Contract tests for the preflight artifact schema (hermetic — no Neo4j).

These pin the producer side of the syncmill integration contract: every fixture
under ``contracts/fixtures/`` must validate against ``contracts/preflight.schema.json``
(except the deliberate major-version fixture, whose *rejection* is the contract),
and no fixture may carry credential-shaped content. syncmill vendors byte-identical
copies of these fixtures and re-validates them in its own hermetic suite.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema
import pytest

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"
FIXTURES = CONTRACTS / "fixtures"

VALID_FIXTURES = [
    "preflight-allow.json",
    "preflight-warn.json",
    "preflight-unresolved-identity.json",
    "preflight-unknown-tool.json",
    "preflight-drift.json",
    "preflight-additive.json",
]

# Keys whose PRESENCE anywhere in an artifact violates the data-classification
# contract — bodies and secrets live outside the artifact, referenced by digest.
_FORBIDDEN_KEYS = {"prompt", "output", "stdout", "stderr", "patch", "env", "credential", "password", "token", "api_key"}

# Credential shapes that must never appear in an artifact, per the ecosystem
# redaction contract (resource URIs may embed userinfo/query secrets).
_FORBIDDEN_PATTERNS = [
    re.compile(r"://[^/\s]+:[^/\s]+@"),  # URI userinfo user:pass@
    re.compile(r"password\s*=", re.IGNORECASE),
    re.compile(r"token\s*=", re.IGNORECASE),
    re.compile(r"api[_-]?key", re.IGNORECASE),
]


def _walk(node, keys: list, values: list) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            keys.append(key)
            _walk(value, keys, values)
    elif isinstance(node, list):
        for item in node:
            _walk(item, keys, values)
    else:
        values.append(node)


def _scan(doc) -> list[str]:
    """Structured redaction scan: forbidden keys (case-insensitive), absolute-path
    values, and credential-shaped strings. Returns a list of violations."""
    keys: list = []
    values: list = []
    _walk(doc, keys, values)
    violations = [f"key:{k}" for k in keys if k.lower() in _FORBIDDEN_KEYS]
    for v in values:
        if isinstance(v, str):
            if v.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", v):
                violations.append(f"abs-path:{v}")
            for pattern in _FORBIDDEN_PATTERNS:
                if pattern.search(v):
                    violations.append(f"cred:{v}")
    return violations


def _schema() -> dict:
    return json.loads((CONTRACTS / "preflight.schema.json").read_text(encoding="utf-8"))


def _validator() -> jsonschema.Draft202012Validator:
    schema = _schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


@pytest.mark.parametrize("name", VALID_FIXTURES)
def test_fixture_validates(name: str):
    doc = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    _validator().validate(doc)


def test_v2_fixture_is_rejected():
    """An unknown major schema_version must NOT validate — consumers reject it."""
    doc = json.loads((FIXTURES / "preflight-v2.json").read_text(encoding="utf-8"))
    assert doc["schema_version"] == 2
    errors = list(_validator().iter_errors(doc))
    assert any(list(e.absolute_path) == ["schema_version"] for e in errors)


def test_additive_fixture_carries_unknown_fields():
    """The tolerance fixture must actually contain fields the schema doesn't know."""
    doc = json.loads((FIXTURES / "preflight-additive.json").read_text(encoding="utf-8"))
    known = set(_schema()["properties"])
    assert set(doc) - known, "additive fixture no longer has any unknown field"


def test_decision_matches_shape():
    """decision derivation contract: agent_found=false -> unresolved_identity;
    rejected rows -> advisory_warn; otherwise advisory_allow."""
    for name in VALID_FIXTURES:
        doc = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        if not doc["agent_found"]:
            assert doc["decision"] == "unresolved_identity"
        elif doc["rejected"]:
            assert doc["decision"] == "advisory_warn"
        else:
            assert doc["decision"] == "advisory_allow"


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.name)
def test_no_forbidden_content(path: Path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert _scan(doc) == [], f"{path.name} violates redaction contract"


def test_scanner_catches_planted_violations():
    """Adversarial guard: the scanner must FLAG a planted secret / body / abs path,
    including uppercase keys and Windows paths — else the clean-fixture scan is vacuous."""
    poisoned = {
        "schema_version": 1,
        "PROMPT": "leak the user's prompt",
        "rejected": [{"candidate": "x", "reason": "DRIFTED", "paths": ["C:\\Users\\me\\secret"]}],
        "note": "postgres://user:pass@host/db",
    }
    found = _scan(poisoned)
    assert any(v.startswith("key:PROMPT") for v in found)
    assert any(v.startswith("abs-path:") for v in found)
    assert any(v.startswith("cred:") for v in found)

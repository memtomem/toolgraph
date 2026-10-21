"""Validation errors print schema locations and fixed messages, never input (#221)."""

from __future__ import annotations

import http.server
import json
import sys
import threading
from pathlib import Path

import pytest
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_REQUEST, PARSE_ERROR
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from typer.testing import CliRunner

from toolgraph import cli
from toolgraph.crawler.crawl import _describe, crawl_all
from toolgraph.error_text import UNPARSED_REPLY, describe_error, describe_validation_error
from toolgraph.errors import ValueFreeError
from toolgraph.models import Governance, ServersConfig, ServerSpec
from toolgraph.review_candidates import (
    ReviewCandidateError,
    annotate_candidate,
    default_annotations_path,
    list_candidates,
)

ROOT = Path(__file__).parents[1]
GOLDEN = ROOT / "contracts" / "tracegraph" / "v1.json"
CANDIDATE_ID = "167379e8-3436-546b-9dbb-5fa087ab0493"
MALFORMED_SERVER = str(Path(__file__).parent / "fixtures" / "malformed_list_server.py")
MARKERS = ["neo4j://u:QX ZQ@h/r", "QXZQ_KEY", "password=QXZQ", "postgres://u:prefix QXZQ@h/x"]


def _assert_hidden(text: str) -> None:
    assert "QX" not in text
    assert "ZQ" not in text


def _error(model: type[BaseModel], data: object) -> ValidationError:
    with pytest.raises(ValidationError) as raised:
        model.model_validate(data)
    return raised.value


def _planted_inputs() -> list[tuple[str, type[BaseModel], dict]]:
    """A marker in every mapping-key and unknown-field position, and in values."""
    cases = []
    for m in MARKERS:
        cases += [
            ("env key", ServersConfig, {"servers": [{"env": {m: []}}]}),
            ("headers key", ServersConfig, {"servers": [{"headers": {m: 1}}]}),
            ("spec field", ServersConfig, {"servers": [{m: 1}]}),
            ("top-level field", ServersConfig, {"servers": [], m: 1}),
            ("transport", ServersConfig, {"servers": [{"transport": m}]}),
            ("args", ServersConfig, {"servers": [{"args": m}]}),
            ("governed_by key", Governance, {"governed_by": {m: "x"}}),
            ("governed_by key", Governance, {"governed_by": {m: [{"policy": 1}]}}),
            ("binding field", Governance, {"governed_by": {"s::t": [{"policy": "p", m: 1}]}}),
            ("top-level field", Governance, {m: 1}),
            ("grant field", Governance, {"grants": [{"agent": "a", "tool": "s::t", m: 1}]}),
            (
                "provenance field",
                Governance,
                {"grants": [{"agent": "a", "tool": "s::t", "provenance": {m: 1}}]},
            ),
            ("effect", Governance, {"policies": [{"id": "p", "effect": m}]}),
            (
                "provenance source",
                Governance,
                {"grants": [{"agent": "a", "tool": "s::t", "provenance": {"source": m}}]},
            ),
        ]
        for field in ("agent", "tool", "policy", "resource", "reason"):
            exception = {"agent": "a", "tool": "s::t", "policy": "p", field: m, m: 1}
            cases.append(
                (f"expected_exceptions {field}", Governance, {"expected_exceptions": [exception]})
            )
        cases += [
            (
                "data_access mode",
                Governance,
                {"data_access": [{"tool": "s::t", "resource": "r", "mode": m}]},
            ),
            (
                "data_access field",
                Governance,
                {"data_access": [{"tool": "s::t", "resource": "r", "mode": "READS", m: 1}]},
            ),
            ("policy field", Governance, {"policies": [{"id": "p", "effect": "DENY", m: 1}]}),
            ("server url", ServersConfig, {"servers": [{"url": [m]}]}),
            ("server command", ServersConfig, {"servers": [{"command": {m: 1}}]}),
        ]
        if m != "QXZQ_KEY":  # a plain identifier is a valid name
            cases += [
                ("server name", ServersConfig, {"servers": [{"name": m}]}),
                ("agent", Governance, {"agents": [m]}),
            ]
    return cases


@pytest.mark.parametrize(("position", "model", "data"), _planted_inputs())
def test_no_planted_marker_reaches_the_rendered_error(position, model, data):
    exc = _error(model, data)
    _assert_hidden(describe_validation_error(exc, model))
    _assert_hidden(describe_error(exc))


@pytest.mark.parametrize(
    ("model", "data", "expected"),
    [
        (
            Governance,
            {"governed_by": {"postgres://u:prefix QXZQ@h/x": [{"policy": 1}]}},
            "2 validation errors for Governance: "
            "governed_by.<key>.0.str: expected a string [type=string_type]; "
            "governed_by.<key>.0.GovernedByBinding.policy: expected a string [type=string_type]",
        ),
        (
            Governance,
            {"governed_by": {"postgres://u:prefix QXZQ@h/x": "notalist"}},
            "1 validation error for Governance: "
            "governed_by.<key>: expected a list [type=list_type]",
        ),
        (
            ServersConfig,
            {"servers": [{"env": {"QXZQ_KEY": []}}]},
            "1 validation error for ServersConfig: "
            "servers.0.env.<key>: expected a string [type=string_type]",
        ),
        (
            Governance,
            {"neo4j://u:QX ZQ@h/r": 1},
            "1 validation error for Governance: <key>: unknown field [type=extra_forbidden]",
        ),
        (
            ServersConfig,
            {"servers": [{"env": {1: "x"}}]},
            "1 validation error for ServersConfig: "
            "servers.0.env.<key>.[key]: expected a string [type=string_type]",
        ),
        (
            Governance,
            {"governed_by": {"s::t": [{"policy": "p", "QXZQ": 1}]}},
            "2 validation errors for Governance: "
            "governed_by.<key>.0.str: expected a string [type=string_type]; "
            "governed_by.<key>.0.GovernedByBinding.<key>: unknown field [type=extra_forbidden]",
        ),
        (
            Governance,
            {"grants": [{"agent": "a", "tool": "s::t", "provenance": {"QXZQ": 1}}]},
            "1 validation error for Governance: "
            "grants.0.provenance.<key>: unknown field [type=extra_forbidden]",
        ),
        (
            Governance,
            {"policies": [{"id": "p", "effect": "QXZQ"}]},
            "1 validation error for Governance: "
            "policies.0.effect: Input should be 'ALLOW' or 'DENY' [type=literal_error]",
        ),
        (
            Governance,
            {"governed_by": {"srv::neo4j://u:QXZQ@h": ["p"]}},
            "1 validation error for Governance: "
            "governed_by tool ref must not contain credential-shaped data [type=value_error]",
        ),
        (
            ServersConfig,
            {},
            "1 validation error for ServersConfig: servers: field required [type=missing]",
        ),
    ],
    ids=[
        "union-labels",
        "mapping-key",
        "env-key",
        "top-level-field",
        "non-string-key",
        "nested-field",
        "provenance-field",
        "literal",
        "model-validator",
        "missing",
    ],
)
def test_location_keeps_schema_parts_and_masks_the_rest(model, data, expected):
    assert describe_validation_error(_error(model, data), model) == expected


def test_without_a_model_no_location_and_no_schema_message():
    exc = _error(Governance, {"policies": [{"id": "p", "effect": "QXZQ"}], "QXZQ": 1})

    assert describe_error(exc) == (
        "2 validation errors for Governance: invalid value [type=literal_error]; "
        "unknown field [type=extra_forbidden]"
    )


def test_identity_refusal_keeps_its_message_without_a_model():
    exc = _error(Governance, {"agents": ["neo4j://u:QXZQ@h"]})

    assert describe_error(exc) == (
        "1 validation error for Governance: "
        "agent must not contain credential-shaped data [type=value_error]"
    )


class _Lookalike(BaseModel):
    """A foreign model that claims a Toolgraph title."""

    model_config = ConfigDict(title="Governance")

    agents: list[str]

    @field_validator("agents")
    @classmethod
    def _quote(cls, value: list[str]) -> list[str]:
        raise ValueError(f"bad agent {value[0]}")


def test_a_title_is_not_trusted_and_only_value_free_messages_are_printed():
    exc = _error(_Lookalike, {"agents": ["QXZQ"]})

    assert exc.title == "Governance"
    assert (
        describe_error(exc) == "1 validation error for Governance: invalid value [type=value_error]"
    )


def test_a_value_free_error_from_any_model_is_printed_as_written():
    class Plain(BaseModel):
        name: str

        @field_validator("name")
        @classmethod
        def _refuse(cls, value: str) -> str:
            raise ValueFreeError("name is refused")

    assert describe_error(_error(Plain, {"name": "QXZQ"})) == (
        "1 validation error for Plain: name is refused [type=value_error]"
    )


def test_other_errors_still_go_through_redact_text():
    assert describe_error(RuntimeError("password=QXZQ")) == "password=<redacted>"


# --- CLI print sites ------------------------------------------------------


def _write(tmp_path: Path, name: str, data: dict) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


GOVERNANCE_KEY_ERROR = {"agents": ["bot"], "governed_by": {"postgres://u:prefix QXZQ@h/x": "x"}}
GOVERNANCE_DETAIL = (
    "1 validation error for Governance: governed_by.<key>: expected a list [type=list_type]"
)


def test_crawl_prints_a_servers_config_error_without_keys(tmp_path, monkeypatch):
    path = _write(tmp_path, "servers.yaml", {"servers": [{"env": {"QXZQ_KEY": []}}]})
    monkeypatch.setattr(cli, "crawl_all", lambda *a, **k: pytest.fail("must not crawl"))

    result = CliRunner().invoke(cli.app, ["crawl", "--servers", str(path)])

    assert result.exit_code == 1
    assert result.output.strip() == (
        f"Invalid servers config at {path}: 1 validation error for ServersConfig: "
        "servers.0.env.<key>: expected a string [type=string_type]"
    )


@pytest.mark.parametrize("dry_run", [False, True], ids=["ingest", "dry-run-json"])
def test_ingest_manifest_prints_a_governance_error_without_keys(tmp_path, dry_run):
    path = _write(tmp_path, "governance.yaml", GOVERNANCE_KEY_ERROR)
    args = ["ingest-manifest", "--governance", str(path)]

    result = CliRunner().invoke(cli.app, args + (["--dry-run", "--json"] if dry_run else []))

    assert result.exit_code == 1
    assert result.output.strip() == f"Invalid governance config at {path}: {GOVERNANCE_DETAIL}"


def test_policy_impact_renders_the_governance_location(tmp_path):
    path = _write(tmp_path, "governance.yaml", GOVERNANCE_KEY_ERROR)

    result = CliRunner().invoke(cli.app, ["policy", "impact", str(path), "s::t", "--agent", "a"])

    assert result.exit_code == 1
    assert result.output.strip() == f"ERROR: {GOVERNANCE_DETAIL}"


def test_policy_propose_prints_no_location_it_cannot_place(tmp_path):
    path = _write(tmp_path, "governance.yaml", GOVERNANCE_KEY_ERROR)

    result = CliRunner().invoke(
        cli.app, ["policy", "propose", str(path), str(tmp_path / "observed.jsonl")]
    )

    assert result.exit_code == 1
    assert result.output.strip() == (
        "ERROR: 1 validation error for Governance: expected a list [type=list_type]"
    )


# --- crawl failures -------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("hint", "expected a boolean [type=bool_parsing]"),
        (
            "schema",
            "expected a mapping [type=dict_type]; expected a boolean [type=bool_parsing]",
        ),
        ("noname", "field required [type=missing]"),
    ],
    ids=["hint", "schema", "noname"],
)
def test_a_malformed_server_reply_fails_without_its_content(mode, expected):
    spec = ServerSpec(
        name="malformed", transport="stdio", command=sys.executable, args=[MALFORMED_SERVER, mode]
    )

    results, failures = crawl_all([spec], timeout=30)

    assert results == []
    [(_, error)] = failures
    count = "2 validation errors" if mode == "schema" else "1 validation error"
    assert error == f"ValidationError: {count} for ListToolsResult: {expected}"


def test_a_nested_exception_group_renders_each_leaf():
    validation = _error(ServersConfig, {"servers": [{"env": {"QXZQ_KEY": []}}]})
    group = ExceptionGroup(
        "outer",
        [ExceptionGroup("inner", [validation, RuntimeError("password=QXZQ")]), OSError("gone")],
    )

    assert _describe(group) == (
        "ValidationError: 1 validation error for ServersConfig: "
        "expected a string [type=string_type]; RuntimeError: password=<redacted>; OSError: gone"
    )


# --- review-candidate sidecar ---------------------------------------------


def _sidecar(tmp_path: Path) -> tuple[Path, dict]:
    source = tmp_path / "candidates.json"
    source.write_bytes(GOLDEN.read_bytes())
    annotate_candidate(source, CANDIDATE_ID, disposition="accepted", reviewer="alice")
    sidecar = default_annotations_path(source)
    return source, json.loads(sidecar.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("plant", "location"),
    [
        (lambda doc, key: doc.__setitem__(key, 1), "<key>"),
        (lambda doc, key: doc["source"].__setitem__(key, 1), "source.<key>"),
        (lambda doc, key: doc["events"][0].__setitem__(key, 1), "events.0.<key>"),
        (
            lambda doc, key: doc["events"][0]["candidate"].__setitem__(key, 1),
            "events.0.candidate.<key>",
        ),
    ],
    ids=["top-level", "source", "event", "candidate"],
)
def test_sidecar_unknown_key_is_masked(tmp_path, plant, location):
    source, document = _sidecar(tmp_path)
    plant(document, "neo4j://u:QX ZQ@h/r")
    default_annotations_path(source).write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ReviewCandidateError) as raised:
        list_candidates(source)

    assert str(raised.value) == f"invalid review annotations at {location}: unknown field"


def test_sidecar_validator_message_is_kept(tmp_path):
    source, document = _sidecar(tmp_path)
    document["events"][0]["candidate_id"] = "QXZQ"
    default_annotations_path(source).write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ReviewCandidateError) as raised:
        list_candidates(source)

    assert (
        str(raised.value) == "invalid review annotations at events.0.candidate_id: must be a UUID"
    )


# --- shortened reprs (#236) -----------------------------------------------


def _int_error(value: str | bytes, base: int = 10) -> ValueError:
    with pytest.raises(ValueError) as raised:
        int(value, base)
    return raised.value


@pytest.mark.parametrize(
    ("value", "base"),
    [
        ("password\t=QXZQ" + "x" * 250, 10),
        (b"password\t=QXZQ" + b"x" * 250, 10),
        ("password\xa0=QXZQ", 10),
        ("password\t=QXZQ", 16),
    ],
    ids=["str-cut", "bytes-cut", "short", "base-16"],
)
def test_an_int_error_is_shown_without_its_value(value, base):
    exc = _int_error(value, base)
    assert "QXZQ" in str(exc)

    assert describe_error(exc) == f"invalid literal for int() with base {base} (value not shown)"


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("password=QXZQ"),
        ValueError("invalid literal for int() with base 10: 'password=QXZQ'", "second"),
        type("Custom", (ValueError,), {})(
            "invalid literal for int() with base 10: 'password=QXZQ'"
        ),
    ],
    ids=["other-text", "two-args", "subclass"],
)
def test_other_value_errors_go_to_the_scrubber(exc):
    assert describe_error(exc) != "invalid literal for int() with base 10 (value not shown)"
    _assert_hidden(describe_error(exc))


def test_an_mcp_parse_error_is_shown_as_fixed_text():
    exc = MCPError(PARSE_ERROR, "Failed to parse JSON response: input_value='password\\t=QXZQ'")

    assert describe_error(exc) == UNPARSED_REPLY


def test_an_mcp_error_with_another_code_goes_to_the_scrubber():
    assert describe_error(MCPError(INVALID_REQUEST, "password=QXZQ")) == "password=<redacted>"


# A JSON string where a JSON-RPC message belongs. The SDK copies the pydantic
# error into an MCPError, and pydantic shortens input_value to 25 + "..." + 24
# repr characters: the cut can split an escape, or drop the quote that made
# repr choose its quote character.
UNPARSABLE_REPLIES = {
    **{
        f"split-escape-{n}": "password\t=QXZQ" + "y" * n + "\x01" + "z" * 80
        for n in (5, 6, 7, 8, 9)
    },
    "lost-quote": "password\t=QXZQ" + "a" * 40 + "'" + "b" * 80,
    "whole-escape": "password\t=QXZQ" + "z" * 80,
}


class _JSONStringServer(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["content-length"]))
        self.server.posts += 1
        body = json.dumps(self.server.reply).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self):
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def json_string_server(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _JSONStringServer)
    server.posts = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


@pytest.mark.parametrize("reply", UNPARSABLE_REPLIES.values(), ids=UNPARSABLE_REPLIES.keys())
def test_an_unparsable_http_reply_fails_without_its_content(json_string_server, reply):
    json_string_server.reply = reply
    url = f"http://127.0.0.1:{json_string_server.server_port}/mcp"
    spec = ServerSpec(name="unparsable", transport="streamable-http", url=url)

    results, failures = crawl_all([spec], timeout=30)

    assert results == []
    [(failed, error)] = failures
    assert failed is spec
    assert json_string_server.posts >= 1
    assert error == f"MCPError: {UNPARSED_REPLY}"

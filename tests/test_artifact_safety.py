"""Artifact identity safety: identities survive unchanged, credential-shaped
ones are refused before any graph read, evidence is redacted.
"""

from __future__ import annotations

import itertools
import json
import time
import warnings

import pytest
import yaml
from typer.testing import CliRunner

from tests._helpers import FakeBundleReader
from toolgraph import cli
from toolgraph.artifact_safety import EvidenceUriError, display_resource, redact, safe_artifact
from toolgraph.graph import loader, queries
from toolgraph.manifest.ingest import ingest_governance
from toolgraph.models import CrawlResult, Governance, ToolRecord
from toolgraph.policy_bundle import build_policy_bundle
from toolgraph.preflight import build_preflight
from toolgraph.uris import normalize_resource_uri


def test_identity_preservation_and_evidence_redaction():
    identity = "https://identity.test/a?role=one#part"
    artifact = {
        "agent": identity,
        "eligible": [identity],
        "rejected": [
            {
                "candidate": identity,
                "candidates": [identity],
                "paths": ["https://user:secret@host/r?token=secret#secret"],
            }
        ],
        "run_id": identity,
    }
    result = safe_artifact(artifact)
    assert (
        result["agent"]
        == result["eligible"][0]
        == result["rejected"][0]["candidate"]
        == result["run_id"]
        == identity
    )
    assert result["rejected"][0]["paths"] == ["https://host/r"]
    assert artifact["rejected"][0]["paths"][0].startswith("https://user:")


@pytest.mark.parametrize(
    "identity",
    [
        "https://u:p@host/a",
        "s::t?token=secret",
        "https://me@corp.example:p@host/a",
        "s::t?access_token=secret",
        "s::t?x=1&credential=secret",
    ],
)
def test_credentials_rejected_before_graph_access(monkeypatch, identity):
    monkeypatch.setattr(queries, "with_graph_state", lambda *a, **kw: pytest.fail("graph accessed"))
    with pytest.raises(ValueError, match="credential"):
        build_preflight(agent="a", candidates=[identity], profile="review", run_id="r")
    with pytest.raises(ValueError, match="credential"):
        build_policy_bundle(agent=identity, store=FakeBundleReader(0))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('neo4j://u:"hunter 2"@host/r', "neo4j://<redacted-uri>"),
        ('neo4j://u:"p)ss"@host/r', "neo4j://<redacted-uri>"),
        ("neo4j://u:'p w'@host/r", "neo4j://<redacted-uri>"),
        (
            '(t) -READS-> (neo4j://u:"p w"@Host/DB) -GOVERNED_BY-> (p:DENY)',
            "(t) -READS-> (neo4j://<redacted-uri>",
        ),
        ('neo4j://u:"pw@x/y"@host/r', "neo4j://<redacted-uri>"),
        ('neo4j://"u s":"p"@h/r', "neo4j://<redacted-uri>"),
        ('neo4j://"u":\'p"@LEAK/tail\'@host/r', "neo4j://<redacted-uri>"),
        ('https://u:SECRET://"pw"@host/r', "https://<redacted-uri>"),
        ('https://u:LEAK!x://"p w" @host/r', "https://<redacted-uri>"),
        ('1https://u:SECRET://"p w"@h/r', "1https://<redacted-uri>"),
        ('neo4j://u:LEAK/"first x://"tail"@host/r', "neo4j://<redacted-uri>"),
        ('neo4j://u:LEAK/"first\tx://"tail"@host/r', "neo4j://<redacted-uri>"),
        ('neo4j://u:"prefix@LEAK/tail"', "neo4j://<redacted-uri>"),
        ('neo4j://u:"prefix@LEAK/tail"%40host/r', "neo4j://<redacted-uri>"),
        ('neo4j://u:"pw"@Host/DB?token=prefix@SECRET', "neo4j://<redacted-uri>"),
        ('neo4j://u:"pw"@Host/DB#prefix@PRIVATE', "neo4j://<redacted-uri>"),
        ('https://Host/r?token="prefix"@SECRET', "https://<redacted-uri>"),
        ("https://Host/r#'prefix'@PRIVATE", "https://<redacted-uri>"),
        ('neo4j://u:"p?w"@Host/DB', "neo4j://<redacted-uri>"),
        (
            'note :// neo4j://u:"pw"@Host/DB?token=SECRET#PRIVATE',
            "note :// neo4j://<redacted-uri>",
        ),
        ('123:// neo4j://u:"pw"@Host/DB?token=SECRET', "123:// neo4j://<redacted-uri>"),
        # Everything after the first scheme goes, even another resource or a
        # credential-free value: a quoted userinfo's end cannot be found.
        (
            '(t) -R-> (https://a/x) -W-> (neo4j://u:"p w"@h/r)',
            "(t) -R-> (https://<redacted-uri>",
        ),
        ('https://exa"mple/a@b', "https://<redacted-uri>"),
        # Graph resource URIs arrive percent-encoded (normalize_resource_uri).
        ("neo4j://u:%22pw@x/y%22@host/r", "neo4j://<redacted-uri>"),
        ("neo4j://u:%22p)ss%22@host/r", "neo4j://<redacted-uri>"),
        ("neo4j://u:%22prefix@LEAK/tail%22", "neo4j://<redacted-uri>"),
        ("neo4j://u:%27pw%27@Host/DB?token=prefix@SECRET", "neo4j://<redacted-uri>"),
    ],
)
def test_quoted_userinfo_hides_everything_after_the_scheme(value, expected):
    assert redact(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # No '@' after the quote: no userinfo, so the usual rule applies.
        ('neo4j://u:"pw"host/r', 'neo4j://u:"pw"host/r'),
        ('(t) -READS-> (https://Host/a?q="x")', "(t) -READS-> (https://Host/a)"),
        # A quote before the scheme is ordinary text.
        ('"quoted" a@b https://u:p@Host/a', '"quoted" a@b https://Host/a'),
        ('123://u:"p w"@h/r', '123://u:"p w"@h/r'),
        ("file:///a%22b/c%27d", "file:///a%22b/c%27d"),
        ("neo4j://u:%20pw@Host/DB", "neo4j://Host/DB"),
        ("neo4j://u:pw@Host:7687/DB?token=x#f", "neo4j://Host:7687/DB"),
        ("x (mcp://user:pw@CaseHost:8080/r?a=b#frag) y", "x (mcp://CaseHost:8080/r) y"),
        ("HTTPS://u:p@H/r?x=1", "https://H/r"),
        ("file:///shared/Private.txt", "file:///shared/Private.txt"),
        ("stdio:npx", "stdio:npx"),
        ("plain text, no uri", "plain text, no uri"),
        ("1https://u:p@h/r", "1https://h/r"),
        ("123://https://u:p@h/r", "123://https://h/r"),
        ("123://https://Host/R?q=x", "123://https://Host/R"),
    ],
)
def test_other_values_keep_main_redaction(value, expected):
    assert redact(value) == expected


def test_nested_evidence_hides_quoted_userinfo_and_keeps_identities():
    artifact = {
        "agent": "codex",
        "rejected": [
            {
                "tool_key": "srv::write",
                "paths": [{"resource": "neo4j://u:'p w'@Host/DB", "policy": "p"}],
            }
        ],
    }
    assert safe_artifact(artifact) == {
        "agent": "codex",
        "rejected": [
            {
                "tool_key": "srv::write",
                "paths": [{"resource": "neo4j://<redacted-uri>", "policy": "p"}],
            }
        ],
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # A ')' in userinfo of a whole value is stripped with it: the value is
        # bounded, so nothing guesses where the password ends.
        ("neo4j://u:p)ss@host/r", "neo4j://host/r"),
        ("neo4j://u:p(ss)@h/r", "neo4j://h/r"),
        ("file:///a(b)@c", "file://<redacted-uri>"),
        ("https://h/x)?e=a@b", "https://<redacted-uri>"),
        ('neo4j://u:"p)ss"@host/r', "neo4j://<redacted-uri>"),
        # #212: a whole value loses its query whole, ')' and all.
        ("https://h/r?k=QX)ZQ", "https://h/r"),
    ],
)
def test_parenthesized_userinfo_is_stripped_with_the_value(value, expected):
    assert redact(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # In free text a ')' then an '@' hides everything after the scheme: the
        # password may go on past the whitespace, so the parser's host may be
        # part of it.
        ("see https://u:)@QXZQ tail@h/r", "see https://<redacted-uri>"),
        ("see https://u:QX)ZQ@h tail", "see https://<redacted-uri>"),
        ("see https://u:)@QXZQ\u3000tail@h/r", "see https://<redacted-uri>"),
        (
            "(t) -READS-> (neo4j://u:p)ss@host/r) -GOVERNED_BY-> (p:DENY)",
            "(t) -READS-> (neo4j://<redacted-uri>",
        ),
        (
            "(t) -READS-> (https://host/a) -GOVERNED_BY-> (neo4j://u:p)ss@h/r:DENY)",
            "(t) -READS-> (https://host/a) -GOVERNED_BY-> (neo4j://<redacted-uri>",
        ),
        ("(https://a)(https://u:p@b)", "(https://<redacted-uri>"),
    ],
)
def test_parenthesized_userinfo_in_free_text_hides_the_rest_of_the_text(value, expected):
    assert redact(value) == expected
    assert safe_artifact({"exception_reason": value}) == {"exception_reason": expected}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Every URI in a run is redacted, the text after a ')' read on.
        ("see https://a)(https://h/r?x=QXZQ)", "see https://a)(https://h/r)"),
        ("see https://a)(https://h/r#QXZQ)", "see https://a)(https://h/r)"),
        (
            "(t) -R-> (https://h/r) -X-> (https://u:p@h2/r?q=1) end",
            "(t) -R-> (https://h/r) -X-> (https://h2/r) end",
        ),
        # Only a value already in its displayed form is exempt from hiding the
        # rest; a placeholder written into a value is not.
        ('see https://h/@scope/password="a<redacted-uri> HUNTER2"', "see https://<redacted-uri>"),
        ("see https://h/@s/token=[a<redacted-uri> HUNTER2]", "see https://<redacted-uri>"),
        ("see https://<redacted-uri> tail", "see https://<redacted-uri> tail"),
    ],
)
def test_free_text_redacts_each_uri_in_a_run(value, expected):
    assert redact(value) == expected


@pytest.mark.parametrize(
    ("resource", "shown"),
    [
        ("neo4j://u:p)ss@host/r", "neo4j://host/r"),
        ("https://h/r?k=QX)ZQ", "https://h/r"),
        ("neo4j://u:QX/ZQ@host/r", "neo4j://<redacted-uri>"),
        ("https://registry/@scope/pkg", "https://<redacted-uri>"),
    ],
)
def test_a_path_built_from_a_resource_keeps_its_line(resource, shown):
    # The formatter displays the resource whole; free-text redaction of the
    # path then keeps the rest of the line, placeholder included.
    path = queries._resource_path("t", "READS", resource, "p")
    assert redact(path) == f"(t) -READS-> ({shown}) -GOVERNED_BY-> (p:DENY)"


def test_ordinary_userinfo_redaction_in_graph_notation():
    assert redact("(t) -READS-> (neo4j://u:pw@host/r)") == "(t) -READS-> (neo4j://host/r)"


@pytest.mark.parametrize(
    "value",
    [
        "(https://host/a(b)) -R-> (x@y)",
        "(t) -READS-> (file:///data/a(1).csv) -GOVERNED_BY-> (p:DENY)",
        "(a) -CAN_CALL-> (t) [GOVERNED_BY p]",
        "(t) -READS-> (http://[::1]:7687/db) -GOVERNED_BY-> (p:DENY)",
        "export:///sink",
    ],
)
def test_parentheses_and_brackets_without_userinfo_are_kept(value):
    assert redact(value) == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # #212: where an unquoted password ends cannot be told, so an '@'
        # after the authority hides everything after the scheme.
        ("neo4j://u:QX/ZQ@host/r", "neo4j://<redacted-uri>"),
        ("neo4j://u:12/34@host/r", "neo4j://<redacted-uri>"),
        ("https://host/neo4j://u:QXZQ@h/r", "https://<redacted-uri>"),
        ("neo4j://u:p#w@host/r", "neo4j://<redacted-uri>"),
        ("https://u:p?x@host/a", "https://<redacted-uri>"),
        ("https://me@corp.example:p#x@host", "https://<redacted-uri>"),
        ("neo4j://u:QXZQ%40host/r", "neo4j://<redacted-uri>"),
        ("neo4j%3A%2F%2Fu:QX/ZQ@host/r", "<redacted-uri>"),
        ("urn:neo4j://u:QX/ZQ@host/r", "urn:neo4j://<redacted-uri>"),
        (" neo4j://u:QX/ZQ@host/r", " neo4j://<redacted-uri>"),
        # In free text the rest of the text goes too: it may hold the rest of
        # the password, or a value whose name went with the URI.
        ("see neo4j://u:QX/ZQ@host/r and more", "see neo4j://<redacted-uri>"),
        ("see neo4j://u:QXZQ%40host/r", "see neo4j://<redacted-uri>"),
        ("see neo4j%3A%2F%2Fu:QXZQ@h/r)(https://a", "see <redacted-uri>"),
        (
            "(t) -READS-> (neo4j://u:QX/ZQ@host/r) -GOVERNED_BY-> (p:DENY)",
            "(t) -READS-> (neo4j://<redacted-uri>",
        ),
        # The cost: a legitimate '@' after the authority is hidden too.
        ("https://registry/@scope/pkg", "https://<redacted-uri>"),
        ("https://user@host:443/p@q", "https://<redacted-uri>"),
        ("https://host/a%40b", "https://<redacted-uri>"),
        (
            "(a) -CAN_CALL-> (t) -READS-> (https://registry/@scope/pkg)",
            "(a) -CAN_CALL-> (t) -READS-> (https://<redacted-uri>",
        ),
    ],
)
def test_an_at_after_the_authority_hides_the_rest_of_the_value(value, expected):
    assert redact(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://[x/r", "https://<redacted-uri>"),
        ("https://u:SECRETPW@[::1/r", "https://<redacted-uri>"),
        ("https://[notip]/r", "https://<redacted-uri>"),
        ("https://h]/r", "https://<redacted-uri>"),
        ("https://u:SECRETPW@h\uff03x/r", "https://<redacted-uri>"),
        ("https://[x/r?token=SECRETPW)ZQ", "https://<redacted-uri>"),
        ("https://u:SECRETPW/ZQ@h/r)https://[x/r", "https://<redacted-uri>"),
        ("1https://u:SECRETPW@[::1/r", "1https://<redacted-uri>"),
        ("https://u:p[)ss@h/r", "https://<redacted-uri>"),
    ],
)
def test_an_unparseable_whole_value_is_hidden(value, expected):
    # A whole value has no siblings to bound, so it is hidden, not refused.
    assert redact(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "(t) -READS-> (https://u:SECRETPW@[::1/r) -GOVERNED_BY-> (p:DENY)",
        "https://u:SECRETPW[ ZQ@[::1/r",
        "(a) -R-> (https://u:SECRETPW@h/x) -R-> (https://[x/r) tail",
        "see https://u:p[)ss@h/r",
    ],
)
def test_unparseable_uri_in_free_text_refuses_without_quoting_it(value):
    with pytest.raises(EvidenceUriError) as raised:
        redact(value)
    assert f"ERROR: {raised.value}\n" == _REFUSAL
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_unparseable_whole_values_hide_beside_their_siblings():
    assert safe_artifact(
        {"paths": ["neo4j://u:SECRETPW/ZQ@host/r", "https://[x/r", "https://u:p[)ss@h/r"]}
    ) == {"paths": ["neo4j://<redacted-uri>", "https://<redacted-uri>", "https://<redacted-uri>"]}


def test_nesting_limit_is_checked_before_uri_refusal():
    def nested(depth):
        value = "(t) -R-> (https://[x/r)"
        for _ in range(depth):
            value = [value]
        return value

    with pytest.raises(EvidenceUriError):
        redact(nested(100))
    with pytest.raises(ValueError, match="nesting exceeds") as raised:
        redact(nested(101))
    assert not isinstance(raised.value, EvidenceUriError)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Resource positions are whole values even with whitespace in them.
        ({"resource": "https://u:QX) ZQ@[::1/r"}, {"resource": "https://<redacted-uri>"}),
        ({"read_resource": "neo4j://u:QX/ZQ@h/r"}, {"read_resource": "neo4j://<redacted-uri>"}),
        (
            {"resources_created": ["\nneo4j://u:QX/ZQ@host/r", "123://u:QXZQ@h"]},
            {"resources_created": ["\nneo4j://<redacted-uri>", "123://<redacted-uri>"]},
        ),
        (
            {"kind": "governed_by", "node_kind": "resource", "ref": "neo4j://u:QX) ZQ@h/r"},
            {"kind": "governed_by", "node_kind": "resource", "ref": "neo4j://h/r"},
        ),
        (
            {"kind": "resource", "ref": "neo4j://u:QX) ZQ@h/r", "policy": "p"},
            {"kind": "resource", "ref": "neo4j://h/r", "policy": "p"},
        ),
        # A tool ref is not a resource value; free-text rules apply to it.
        (
            {"kind": "tool", "ref": "s::t", "policy": "p"},
            {"kind": "tool", "ref": "s::t", "policy": "p"},
        ),
        # A URI-shaped policy id is displayed like a resource.
        ({"policy": "neo4j://u:QX/ZQ@host/r"}, {"policy": "neo4j://<redacted-uri>"}),
        (
            {"exception_reason": "see neo4j://u:QX/ZQ@host/r"},
            {"exception_reason": "see neo4j://<redacted-uri>"},
        ),
    ],
)
def test_resource_positions_are_whole_values(value, expected):
    assert redact(value) == expected
    assert safe_artifact({"evidence": [value]}) == {"evidence": [expected]}


def test_top_level_resource_fields_are_whole_values():
    # safe_artifact passes the key on; before #212 it scanned these as text.
    assert safe_artifact(
        {"read_resource": "neo4j://u:QX/ZQ@h/r", "write_resource": "https://u:QX) ZQ@[::1/r"}
    ) == {"read_resource": "neo4j://<redacted-uri>", "write_resource": "https://<redacted-uri>"}


@pytest.mark.parametrize(
    "template",
    [
        "neo4j://u:{}@host/r",
        "(t) -READS-> (neo4j://u:{}@host/r) -GOVERNED_BY-> (x:DENY)",
        "(a) -CAN_CALL-> (t) -WRITES-> (https://u:{}@h)",
    ],
)
def test_planted_parenthesized_passwords_never_survive(template):
    for size in range(1, 4):
        for chars in itertools.product("a)(@/:?#%][", repeat=size):
            if ")" not in chars:
                continue
            value = template.format("QX" + "".join(chars) + "ZQ")
            try:
                out = redact(value)
            except EvidenceUriError:
                continue
            assert "QX" not in out and "ZQ" not in out, (value, out)


@pytest.mark.parametrize(
    "value",
    [
        "a" * 100_000,
        "a://" * 25_000,
        'n://"@' * 20_000,
        '"' * 100_000 + "://",
        "1" * 100_000,
        "a://)" * 20_000,
        "x://" + ")" * 100_000,
        "https://h/ " * 9_000,
        "%3A" * 60_000,
        "x%3a%2f%2f" * 20_000,
        "(https://h/" * 18_000,
        "a://u:" * 30_000 + "@",
    ],
)
def test_redaction_is_linear(value):
    started = time.perf_counter()
    redact(value)
    assert time.perf_counter() - started < 0.5


_UNPARSEABLE = ["https://u:SECRETPW@[::1/r", "https://u:SECRETPW@h\uff03x/r"]
_REFUSAL = "ERROR: artifact evidence holds a URI that cannot be parsed; it is not shown\n"


def _seed_tool():
    loader.load_crawl_result(
        CrawlResult(server_name="s", transport="stdio", tools=[ToolRecord(name="t")])
    )


def _governance(resource: str, *, governed: bool = True) -> dict:
    return {
        "agents": ["bot"],
        "policies": [{"id": "deny1", "effect": "DENY", "scope": "secrets"}],
        "grants": [{"agent": "bot", "tool": "s::t", "granted_by": "ops"}],
        "data_access": [{"tool": "s::t", "resource": resource, "mode": "READS"}],
        "governed_by": {resource: ["deny1"]} if governed else {},
    }


def _ingest(resource: str):
    _seed_tool()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # the URI is stored verbatim, by design
        report = ingest_governance(Governance.model_validate(_governance(resource)))
    assert report.applied, report.warnings


# #212 shapes, each with its password marked QX..ZQ. They pass
# normalize_resource_uri unchanged (or percent-encoded) and leaked before.
_PASSWORD_SHAPES = [
    "https://u:QX) ZQ@[::1/r",
    "neo4j://u:QX/ZQ@host/r",
    "neo4j://u:12QX/ZQ@host/r",
    "https://host/neo4j://u:QXZQ@h/r",
    "https://h/r?k=QX)ZQ",
    "neo4j://u:QXZQ%40host/r",
    'neo4j://u:"QX ZQ"@host/r',
]
_MARKERS = ("SECRETPW", "QX", "ZQ")


def _shown(resource: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return display_resource(normalize_resource_uri(resource))


@pytest.mark.parametrize("resource", _UNPARSEABLE + _PASSWORD_SHAPES)
@pytest.mark.parametrize(
    "command",
    [
        ["preflight", "bot", "s::t", "--run-id", "r1"],
        ["preflight", "bot", "s::t", "--run-id", "r1", "--out", "{out}"],
        ["policy", "compile", "--agent", "bot", "--output", "{out}"],
    ],
)
def test_artifact_commands_hide_a_resource_password(backend_graph, tmp_path, resource, command):
    _ingest(resource)
    out = tmp_path / "artifact.json"
    args = [arg.replace("{out}", str(out)) for arg in command]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    text = result.stdout + (out.read_text() if out.exists() else "")
    assert _shown(resource) in text  # the evidence is there, displayed
    for marker in _MARKERS:
        assert marker not in text + result.stderr, (resource, marker)


def _dry_run(tmp_path, governance: dict):
    path = tmp_path / "governance.yaml"
    path.write_text(yaml.safe_dump(governance, allow_unicode=True))
    return CliRunner().invoke(
        cli.app, ["ingest-manifest", "--governance", str(path), "--dry-run", "--json"]
    )


@pytest.mark.parametrize("resource", _UNPARSEABLE + _PASSWORD_SHAPES)
def test_dry_run_json_hides_a_new_resource_password(backend_graph, tmp_path, resource):
    _seed_tool()  # the resource is new, so the delta carries it
    result = _dry_run(tmp_path, _governance(resource))
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["resources_created"] == [_shown(resource)]
    for marker in _MARKERS:
        assert marker not in result.output, (resource, marker)


@pytest.mark.parametrize("resource", _PASSWORD_SHAPES)
def test_dry_run_warnings_hide_a_resource_password(backend_graph, tmp_path, resource):
    _seed_tool()
    governance = _governance(resource)
    governance["governed_by"][resource] = ["deny1", "undefined-policy"]
    governance["expected_exceptions"] = [
        {"agent": "bot", "tool": "s::t", "policy": "deny1", "resource": resource + "-other"}
    ]
    result = _dry_run(tmp_path, governance)
    assert result.exit_code == 1, result.output
    report = json.loads(result.stdout)
    assert len(report["warnings"]) == 2, report["warnings"]
    for marker in _MARKERS:
        assert marker not in result.output, (resource, marker)


@pytest.mark.parametrize("resource", _PASSWORD_SHAPES)
def test_dry_run_provenance_update_hides_a_resource_password(backend_graph, tmp_path, resource):
    _ingest(resource)
    governance = _governance(resource)
    governance["governed_by"] = {
        resource: [{"policy": "deny1", "provenance": {"evidence": "runbook"}}]
    }
    result = _dry_run(tmp_path, governance)
    assert result.exit_code == 0, result.output
    [row] = json.loads(result.stdout)["provenance_modified"]
    assert (row["node_kind"], row["ref"]) == ("resource", _shown(resource))
    for marker in _MARKERS:
        assert marker not in result.output, (resource, marker)

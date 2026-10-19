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
from toolgraph.artifact_safety import EvidenceUriError, redact, safe_artifact
from toolgraph.graph import loader, queries
from toolgraph.manifest.ingest import ingest_governance
from toolgraph.models import CrawlResult, Governance, ToolRecord
from toolgraph.policy_bundle import build_policy_bundle
from toolgraph.preflight import build_preflight


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
        ("neo4j://u:p)ss@host/r", "neo4j://<redacted-uri>"),
        ("neo4j://u:p(ss)@h/r", "neo4j://<redacted-uri>"),
        (
            "(t) -READS-> (neo4j://u:p)ss@host/r) -GOVERNED_BY-> (p:DENY)",
            "(t) -READS-> (neo4j://<redacted-uri> -GOVERNED_BY-> (p:DENY)",
        ),
        (
            "(t) -READS-> (https://host/a) -GOVERNED_BY-> (neo4j://u:p)ss@h/r:DENY)",
            "(t) -READS-> (https://host/a) -GOVERNED_BY-> (neo4j://<redacted-uri>",
        ),
        # The run ends at whitespace, not at a ')': adjacent values go too.
        ("(https://a)(https://u:p@b)", "(https://<redacted-uri>"),
        # No password, but a ')' before an '@' cannot be told from one.
        ("file:///a(b)@c", "file://<redacted-uri>"),
        ("https://h/x)?e=a@b", "https://<redacted-uri>"),
        # The quoted rule runs first and still hides to the end.
        ('neo4j://u:"p)ss"@host/r', "neo4j://<redacted-uri>"),
    ],
)
def test_parenthesized_userinfo_hides_the_rest_of_the_run(value, expected):
    assert redact(value) == expected


def test_parenthesized_rule_keeps_ordinary_userinfo_redaction():
    assert redact("(t) -READS-> (neo4j://u:pw@host/r)") == "(t) -READS-> (neo4j://host/r)"


@pytest.mark.parametrize(
    "value",
    [
        "(https://host/a(b)) -R-> (x@y)",
        "(t) -READS-> (file:///data/a(1).csv) -GOVERNED_BY-> (p:DENY)",
        "(a) -CAN_CALL-> (t) -READS-> (https://registry/@scope/pkg)",
        "(a) -CAN_CALL-> (t) [GOVERNED_BY p]",
        "(t) -READS-> (http://[::1]:7687/db) -GOVERNED_BY-> (p:DENY)",
        "https://host/a%40b",
    ],
)
def test_parentheses_and_brackets_without_userinfo_are_kept(value):
    assert redact(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "https://[x/r",
        "https://u:SECRETPW@[::1/r",
        "https://[notip]/r",
        "https://h]/r",
        "https://u:SECRETPW@h\uff03x/r",
        "(t) -READS-> (https://u:SECRETPW@[::1/r) -GOVERNED_BY-> (p:DENY)",
        # Nothing after the unparseable URI may be printed in its place...
        "https://u:SECRETPW[ ZQ@[::1/r",
        "https://[x/r?token=SECRETPW)ZQ",
        # ...nor anything before it or beside it.
        "https://u:SECRETPW/ZQ@h/r)https://[x/r",
        "(a) -R-> (https://u:SECRETPW@h/x) -R-> (https://[x/r) tail",
        "1https://u:SECRETPW@[::1/r",
        # The ')' rule would hide this one, so the check has to run first.
        "https://u:p[)ss@h/r",
    ],
)
def test_unparseable_uri_refuses_without_quoting_it(value):
    with pytest.raises(EvidenceUriError) as raised:
        redact(value)
    assert f"ERROR: {raised.value}\n" == _REFUSAL
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


@pytest.mark.parametrize("unparseable", ["https://[x/r", "https://u:p[)ss@h/r"])
def test_unparseable_uri_refuses_the_whole_artifact(unparseable):
    # The sibling holds a password the scan cannot bound; only refusing the
    # artifact keeps it out.
    with pytest.raises(EvidenceUriError):
        safe_artifact({"paths": ["neo4j://u:SECRETPW/ZQ@host/r", unparseable]})


def test_nesting_limit_is_checked_before_uri_refusal():
    def nested(depth):
        value = "https://[x/r"
        for _ in range(depth):
            value = [value]
        return value

    with pytest.raises(EvidenceUriError):
        redact(nested(100))
    with pytest.raises(ValueError, match="nesting exceeds") as raised:
        redact(nested(101))
    assert not isinstance(raised.value, EvidenceUriError)


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


@pytest.mark.parametrize("resource", _UNPARSEABLE)
@pytest.mark.parametrize(
    "command",
    [
        ["preflight", "bot", "s::t", "--run-id", "r1"],
        ["preflight", "bot", "s::t", "--run-id", "r1", "--out", "{out}"],
        ["policy", "compile", "--agent", "bot", "--output", "{out}"],
    ],
)
def test_artifact_commands_refuse_an_unparseable_resource(
    backend_graph, tmp_path, resource, command
):
    _ingest(resource)
    out = tmp_path / "artifact.json"
    args = [arg.replace("{out}", str(out)) for arg in command]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert result.stderr == _REFUSAL
    assert not out.exists()


@pytest.mark.parametrize("resource", _UNPARSEABLE)
def test_dry_run_json_refuses_an_unparseable_resource(backend_graph, tmp_path, resource):
    _seed_tool()  # the resource is new, so the delta carries it
    path = tmp_path / "governance.yaml"
    path.write_text(yaml.safe_dump(_governance(resource), allow_unicode=True))
    result = CliRunner().invoke(
        cli.app, ["ingest-manifest", "--governance", str(path), "--dry-run", "--json"]
    )
    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert result.stdout == ""
    # The normalization warning for the stored URI may precede it; it does
    # not quote the URI.
    assert result.stderr.endswith(_REFUSAL)
    assert "SECRETPW" not in result.stderr and "Traceback" not in result.stderr


def test_dry_run_json_hides_a_parenthesized_password(backend_graph, tmp_path):
    _seed_tool()
    path = tmp_path / "governance.yaml"
    path.write_text(yaml.safe_dump(_governance("neo4j://u:p)ss@host/r", governed=False)))
    result = CliRunner().invoke(
        cli.app, ["ingest-manifest", "--governance", str(path), "--dry-run", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["resources_created"] == ["neo4j://<redacted-uri>"]
    assert "p)ss" not in result.output

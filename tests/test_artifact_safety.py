"""Artifact identity safety: identities survive unchanged, credential-shaped
ones are refused before any graph read, evidence is redacted.
"""

from __future__ import annotations

import time

import pytest

from tests._helpers import FakeBundleReader
from toolgraph.artifact_safety import redact, safe_artifact
from toolgraph.graph import queries
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
    "value",
    ["a" * 100_000, "a://" * 25_000, 'n://"@' * 20_000, '"' * 100_000 + "://", "1" * 100_000],
)
def test_redaction_is_linear(value):
    started = time.perf_counter()
    redact(value)
    assert time.perf_counter() - started < 0.5

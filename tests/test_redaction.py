"""Redaction of credentials in error text and of secrets in endpoint labels."""

from __future__ import annotations

import time

import pytest

from toolgraph.crawler.crawl import spec_label
from toolgraph.models import ServerSpec
from toolgraph.redaction import endpoint_label, persisted_endpoint, redact_text


def test_endpoint_labels_never_include_args_or_url_secrets():
    stdio = ServerSpec(command="/usr/local/bin/python", args=["server.py", "--token=super-secret"])
    http = ServerSpec(
        transport="streamable-http",
        url="https://alice:secret@example.test:8443/mcp?token=super-secret#fragment",
    )
    sse = ServerSpec(
        transport="sse",
        url="https://alice:secret@example.test:8443/sse?token=super-secret#fragment",
    )

    assert endpoint_label(stdio) == "stdio:python"
    assert spec_label(stdio) == "stdio:python"
    assert endpoint_label(http) == "https://example.test:8443"
    # SSE URLs carry the same credentials and query tokens as streamable-http.
    assert endpoint_label(sse) == "https://example.test:8443"
    assert persisted_endpoint("stdio", "python server.py --token=secret") == "stdio:python"


def test_error_redaction_removes_urls_and_credential_values():
    message = (
        "failed https://alice:secret@example.test/mcp?token=abc "
        "Authorization: Bearer sk-abc123 password=hunter2 "
        "mcp://bob:pw@mcp.example.test/tools?token=def"
    )

    redacted = redact_text(message)
    assert "alice" not in redacted
    assert "secret" not in redacted.lower().replace("<redacted>", "")
    assert "abc" not in redacted
    assert "sk-abc123" not in redacted
    assert "hunter2" not in redacted
    assert "https://example.test" in redacted
    assert "bob" not in redacted
    assert "def" not in redacted
    assert "mcp://mcp.example.test" in redacted


# Values a scrubber that looks for the end of a value can leave partly
# readable: quoted, escaped, doubled, multi-line or structured values, and
# URIs with quoted userinfo or a malformed scheme. Each pairs the text with
# the fragments that must not appear in the result.
QUOTED_OR_UNBOUNDED_SECRETS = [
    ('neo4j://u:"p@ss"@host:7687', ["p@ss"]),
    ("neo4j://u:'hunter2'@host", ["hunter2"]),
    ('neo4j://u:p"ss@host', ['"ss']),
    ('neo4j://me@corp.example:p"ss@host', ['"ss']),
    ('neo4j://u:"hunter 2"@host', ["hunter"]),
    ("1https://alice:hunter2@host/x", ["hunter2"]),
    ("+https://alice:hunter2@host/x", ["hunter2"]),
    ("http://host:8080?token=abc@SENTINEL", ["SENTINEL"]),
    ('password="hunter 2"', ["hunter"]),
    ("password='hunter 2'", ["hunter"]),
    ('NEO4J_PASSWORD="s3cr3t pass"', ["s3cr3t", " pass"]),
    ('password="unbalanced secret', ["unbalanced", "secret"]),
    ('password="a\\"b c"', ["b c"]),
    ('password="hun"ter2', ["ter2"]),
    ('password="abc""hunter2"', ["hunter2"]),
    ('password="first\nsecond"', ["second"]),
    ('password="first\\\nsecond"', ["second"]),
    ('password="first\\\r\nsecond"', ["second"]),
    ('password="hunter\n2', ["hunter", "\n2"]),
    ('password: {"a":"HUNTER 2"}', ["HUNTER"]),
    ('password: ["HUNTER", "SECOND"]', ["HUNTER", "SECOND"]),
    ('password={\n"value":"hunter2"\n}', ["hunter2"]),
    ('Authorization: "Token abc def"', ["abc", "def"]),
    ('Authorization: "Token a\\"b c"', ["b c"]),
    ('Authorization: "first\nsecond"', ["second"]),
    # A URI rewrite must not take the credential name along with the URI.
    ("1https://password : HUNTER2", ["HUNTER2"]),
    ("+https://secret = HUNTER2", ["HUNTER2"]),
    ("1https://Bearer HUNTER2", ["HUNTER2"]),
    ("123://password : HUNTER2", ["HUNTER2"]),
    ("+++://secret = HUNTER2", ["HUNTER2"]),
    (".-1://Bearer HUNTER2", ["HUNTER2"]),
    # Nor leave a prefix of an enclosing URI that reads as an origin.
    ('https://SENTINEL:x://"pw"@host', ["SENTINEL", "sentinel", "pw"]),
    # Quoted userinfo holding a line break.
    ('neo4j://u:"alpha\rbeta"@host', ["alpha", "beta"]),
    ('neo4j://u:"alpha\nbeta"@host', ["alpha", "beta"]),
    ('neo4j://u:"alpha\x0bbeta"@host', ["alpha", "beta"]),
    ('neo4j://u:"alpha\u2028beta"@host', ["alpha", "beta"]),
]


@pytest.mark.parametrize(("text", "fragments"), QUOTED_OR_UNBOUNDED_SECRETS)
def test_quoted_and_unbounded_secrets_leave_nothing_readable(text, fragments):
    redacted = redact_text(text)

    assert [fragment for fragment in fragments if fragment in redacted] == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A quoted value hides everything after it, never part of the value.
        ('{"password": "x y", "user": "alice"}', '{"password=<redacted>'),
        ("password=hunter2 and more", "password=<redacted> and more"),
        # A URI merely wrapped in quotes is not a quoted password.
        ("Unable to connect to 'neo4j://host:7687' (timeout)",) * 2,
        ('{"url": "neo4j://h"}',) * 2,
        ('see "https://h/x" now', 'see "https://h" now'),
        (
            "Client error for url 'https://host/mcp?token=abc'",
            "Client error for url 'https://host'",
        ),
        ("see https://a:b@c.example/x and mail x@y", "see https://c.example and mail x@y"),
        ("a https://u:p@h1/x b https://h2/y", "a https://h1 b https://h2"),
        ("http://[::1]:8080/x", "http://[::1]:8080"),
        ("see the server log",) * 2,
    ],
)
def test_text_around_and_without_secrets_stays_readable(text, expected):
    assert redact_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "a" * 100_000,
        "http://" + "a:" * 50_000,
        "http://u:" + "@" * 100_000,
        "x://'" * 20_000,
        "+" * 100_000,
        'password="' + "a" * 100_000,
        "password=" * 20_000,
        'Authorization: "' + "a" * 100_000,
    ],
    ids=[
        "letters",
        "colons",
        "ats",
        "quoted-uris",
        "scheme-run",
        "quote",
        "keywords",
        "auth-quote",
    ],
)
def test_redaction_stays_linear_on_adversarial_text(text):
    # A scheme pattern that restarts at every letter is quadratic here.
    started = time.perf_counter()
    redact_text(text)
    assert time.perf_counter() - started < 0.5

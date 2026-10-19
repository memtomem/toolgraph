"""Redaction of credentials in error text and of secrets in endpoint labels."""

from __future__ import annotations

import time

import pytest

from toolgraph.crawler.crawl import spec_label
from toolgraph.models import ServerSpec
from toolgraph.redaction import WITHHELD, endpoint_label, persisted_endpoint, redact_text


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
    # A quoted password after a '?' or '#', or after an email username.
    ('neo4j://u:p?"QX ZQ"@h/a', ["QX", "ZQ"]),
    ('neo4j://u:p#"QX ZQ"@h/a', ["QX", "ZQ"]),
    ('see neo4j://u@example.com:"QX ZQ"@h/a', ["QX", "ZQ"]),
    # A user part quoted from the start, raw or encoded.
    ('neo4j://"u":"QX ZQ"@h/a', ["QX", "ZQ"]),
    ("neo4j://%22u%22:%22QX ZQ%22@h/a", ["QX", "ZQ"]),
    ('neo4j://"u v":"QX ZQ"@h/a', ["QX", "ZQ"]),
    ("neo4j://%22u%20v%22:%22QX ZQ%22@h/a", ["QX", "ZQ"]),
    ("neo4j://%27u%E2%80%83v%27:%27QX ZQ%27@h", ["QX", "ZQ"]),
    ("neo4j://u?x:%22QX ZQ%22@h/a", ["QX", "ZQ"]),
    ('neo4j://u#x:"QX ZQ"@h/a', ["QX", "ZQ"]),
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
        'a://u:"' * 30_000 + "@",
        "a://" + ":" * 150_000 + "@",
        'a://"' + ":" * 150_000 + "@",
        "a://u?" * 40_000 + "@",
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
        "quoted-passwords",
        "password-colons",
        "quoted-user-colons",
        "query-users",
    ],
)
def test_redaction_stays_linear_on_adversarial_text(text):
    # A scheme pattern that restarts at every letter is quadratic here.
    started = time.perf_counter()
    redact_text(text)
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    "text",
    [
        "o.jsonl:1: calls must be an integer, got '?%74oken=SECRET'",
        # A scrubbed neighbour must not hide the encoded one from inspection.
        "?%74oken=SECRET&password=x",
        # Nor may a placeholder the input itself carries.
        "?%74oken=SECRET=<redacted>",
        "agent '%70assword=SECRET' not found",
        # One scrub pass must not swallow what the next would have shown.
        "credential=password=x ?%74oken=SECRET",
        "password=Bearer abc ?%74oken=SECRET",
        # An input placeholder in another case must not pass for an inserted one.
        "https%3A//user:SECRET<REDACTED-URI>@host",
        # Scrubbing `token=x@host` must not hide the '@' of an encoded credential URI.
        "https%3A//user:SECRET&token=x@host",
    ],
)
def test_text_the_scrubber_cannot_clean_is_withheld(text):
    assert redact_text(text) == WITHHELD


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Bearer abc expired", "Bearer <redacted> expired"),
        (
            "Bearer authentication failed; retry with a new token",
            "Bearer <redacted> failed; retry with a new token",
        ),
        ("NEO4J_PASSWORD is not set",) * 2,
        ("password=hunter2 and more", "password=<redacted> and more"),
        ("token::refresh failed", "token=<redacted> failed"),
        ("credential=password =hunter2", "credential=<redacted>"),
        # A placeholder the input carries is shown changed, so it can never
        # pass for one the scrubber inserted.
        ("a literal <redacted> marker", "a literal \u2039redacted> marker"),
        ("a literal <REDACTED> marker", "a literal \u2039REDACTED> marker"),
        # Percent-encoding without a credential behind it is left alone.
        ("file /data/%20report.csv not found",) * 2,
        ("url https://h/p%20q?page=2", "url https://h"),
    ],
)
def test_scrubbed_messages_are_not_withheld(text, expected):
    assert redact_text(text) == expected


def test_credential_uri_check_stays_linear():
    # `[^/\s]+:` before the password re-split one authority at every ':'.
    started = time.perf_counter()
    redact_text("123://" + "a:" * 32_000)
    assert time.perf_counter() - started < 0.5


def test_only_the_exact_inserted_placeholders_count_as_inserted():
    # Neutralising input placeholders already covers other spellings; this
    # keeps the second layer honest on its own.
    from toolgraph.redaction import _INSERTED

    assert _INSERTED.fullmatch("PASSWORD=<redacted>")
    assert _INSERTED.fullmatch("bearer <redacted>")
    assert not _INSERTED.search("<REDACTED-URI>")
    assert not _INSERTED.search("token=<Redacted>")

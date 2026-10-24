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
    # The Authorization header comes last: its value runs to the end of the
    # text, so nothing after it would be left to check.
    message = (
        "failed https://alice:secret@example.test/mcp?token=abc "
        "password=hunter2 mcp://bob:pw@mcp.example.test/tools?token=def "
        "Authorization: Bearer sk-abc123"
    )

    redacted = redact_text(message)
    assert redacted == (
        "failed https://example.test password=<redacted> mcp://mcp.example.test "
        "Authorization=<redacted>"
    )
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
        "http://h/password " * 50_000,
        "http://h/" + "token" * 200_000,
        "http://h/" + "authorization:[" * 50_000,
        "http://h/" + "authorization:x/" * 50_000,
        "http://h/" + "authorization=basic/" * 50_000,
        "http://h/%20pass%77ord " * 20_000,
        "http://h/" + "authorization:x/" * 50_000 + " " * 50_000,
        "http://h/" + "%61uthorization:x/" * 20_000 + " " * 20_000,
        "proxy_authorization:x " * 50_000,
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
        "uri-final-names",
        "uri-name-run",
        "uri-bracket-run",
        "uri-colon-run",
        "uri-scheme-word-run",
        "encoded-uri-names",
        "uri-authorization-names-then-spaces",
        "encoded-uri-authorization-names-then-spaces",
        "prefixed-authorization-names",
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


# A credential name at the end of a URI, its value after the URI's end. The
# URI rewrite would take the name and leave the value (#232).
NAME_AT_URI_END = [
    "fetch http://h/password =QXZQ",
    "fetch http://h/password\t=QXZQ",
    "fetch http://h/password= QXZQ",
    "see http://h/api_key = QXZQ",
    "see http://h/x?token =QXZQ",
    "see http://h/Token =QXZQ",
    "see http://h/x?a=1&access_token =QXZQ",
    "see http://h/db_password: QXZQ",
    "see http://h/secret' = 'QXZQ'",
    "see http://h/credential = QXZQ",
    "see neo4j://h/password : QXZQ",
    "see http://h/authorization: QXZQ",
    "see http://h/authorization: Basic QXZQ",
    "http://h/authorization:Basic QXZQ",
    "http://h/authorization:Token QXZQ",
    "http://h/authorization:Digest QXZQ",
    "http://h/authorization:Basic\r\nQXZQ",
    "see http://h/bearer QXZQ",
    "http://xbearer/bearer QXZQ",
    # A bracketed value that starts inside the URI runs past it.
    "http://h/token=[a, QXZQ] end",
    # A name inside another match's value, or after an earlier match.
    "http://h/secret=abc&password =QXZQ",
    "token=http://h/password =QXZQ",
    "password=x<redacted-uri>http://h/password =QXZQ",
    "token=http://password/password =QXZQ",
    "http://token=password/password =QXZQ",
    "Authorization:http://password/password =QXZQ",
    "TOKEN=HTTP://PASSWORD/PASSWORD\xa0=QXZQ",
    "token=http://password/password\r\n=QXZQ",
    "[token=http://password/password =QXZQ]",
    # Names that share letters: `secre|t|oken`.
    "see http://h/secretoken =QXZQ",
    # Forms the scrubbers read as an assignment and the refusal rule does
    # not (#241).
    "http://h/authorization=Basic QXZQ",
    "http://h/Authorization=Token QXZQ",
    "http://h/x?authorization=Basic QXZQ",
    "http://h/proxy_authorization=Basic QXZQ",
    "http://h/proxy-authorization=Basic QXZQ",
    "see http://h/password :QXZQ",
    "http://h/api_key :QXZQ",
    "http://h/access-token :QXZQ",
    "http://h/secret\t:QXZQ",
    "http://h/password:[one, QXZQ]",
    "http://h/credential:{one: QXZQ}",
    # Percent-encoded names and delimiters, each URI where the rewrite finds
    # it (#241).
    "http://h/pass%77ord :QXZQ",
    "http://h/pass%77ord:[one, QXZQ]",
    "http://h/password%3A[one, QXZQ]",
    "http://h/%20pass%77ord :QXZQ",
    "http://h/%22pass%77ord :QXZQ",
    "http://h/%20password%3A[one, QXZQ]",
    "http://h/password %3AQXZQ",
    "a%20http://h/password :QXZQ",
    "http://h/x%3Fpassword :QXZQ",
    # An Authorization value runs to the end of the text, whatever its scheme
    # word, so any text after the URI is its value (#245).
    "http://h/authorization=Digest username=alice,response=QXZQ",
    "http://h/authorization=Digest+v1 response=QXZQ",
    "GET https://h/x?authorization=Negotiate YIIQXZQ",
    "http://h/" + "authorization:x/" * 3 + " QXZQ",
    # Decoded, this is not credential-shaped, so only the decoded pass of the
    # URI-end check withholds it.
    "http://h/%61uthorization:x/ QXZQ",
]


@pytest.mark.parametrize("text", NAME_AT_URI_END)
def test_a_name_at_a_uri_end_withholds_its_value(text):
    assert redact_text(text) == WITHHELD


# Where an Authorization value ends cannot be told from prose, so everything
# after the name is hidden (#245): scheme parameters, a malformed value, a
# second header, and a name with a prefix.
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Authorization: Digest username=alice, response=QXZQ", "Authorization=<redacted>"),
        (
            'Authorization: Digest username="alice", realm="api", nonce="n1", response="QXZQ"',
            "Authorization=<redacted>",
        ),
        ("Authorization: Digest username = alice, response = QXZQ", "Authorization=<redacted>"),
        ("authorization=Digest username=alice,response=QXZQ", "Authorization=<redacted>"),
        ("Authorization: Negotiate YIIQXZQ abc", "Authorization=<redacted>"),
        (
            "Authorization: AWS4-HMAC-SHA256 Credential=AK/x, Signature=QXZQ",
            "Authorization=<redacted>",
        ),
        (
            'Authorization: OAuth oauth_consumer_key="k", oauth_signature="QXZQ"',
            "Authorization=<redacted>",
        ),
        (
            "Authorization: SCRAM-SHA-256 r=nonce,s=salt,i=4096,c=biws,p=QXZQ",
            "Authorization=<redacted>",
        ),
        ("Authorization: NTLM TlRMTVNTUAABAAAAB4IIog==QXZQ", "Authorization=<redacted>"),
        (
            'status 401: Authorization: Hawk id="d", mac="QXZQ" was rejected',
            "status 401: Authorization=<redacted>",
        ),
        ("Authorization: Basic user QXZQ", "Authorization=<redacted>"),
        ("Authorization: Digest.v1 response=QXZQ", "Authorization=<redacted>"),
        ("Authorization: 1Digest response=QXZQ", "Authorization=<redacted>"),
        ("Authorization:\nAuthorization: Digest response=QXZQ", "Authorization=<redacted>"),
        ("Authorization: Bearer\nAuthorization: Digest response=QXZQ", "Authorization=<redacted>"),
        (
            "request failed\nAuthorization: Digest response=QXZQ\nHost: example.test",
            "request failed\nAuthorization=<redacted>",
        ),
        ("proxy_authorization:QXZQ", "proxy_Authorization=<redacted>"),
        ("HTTP_AUTHORIZATION:QXZQ", "HTTP_Authorization=<redacted>"),
        ("xauthorization:QXZQ", "xAuthorization=<redacted>"),
        ("proxy_authorization:[QXZQ]", "proxy_Authorization=<redacted>"),
        ("proxy_authorization: Basic user QXZQ", "proxy_Authorization=<redacted>"),
        ("{'HTTP_AUTHORIZATION': 'Digest response=QXZQ'}", "{'HTTP_Authorization=<redacted>"),
    ],
)
def test_an_authorization_value_is_hidden_to_the_end_of_the_text(text, expected):
    assert redact_text(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Accepted costs: the text after a value goes too (#245).
        ("Authorization: Bearer abc; retrying in 5s", "Authorization=<redacted>"),
        ("see Authorization=abc123 here", "see Authorization=<redacted>"),
        ('Authorization: Basic "" retry', "Authorization=<redacted>"),
        ("reauthorization:pending", "reAuthorization=<redacted>"),
        ("GET https://h/authorization=abc returned 404", WITHHELD),
        # Unchanged: no value, a Bearer word without the name, nothing after
        # the URI.
        ("Authorization:", "Authorization:"),
        ("bearer QXZQ failed", "bearer <redacted> failed"),
        ("http://h/authorization=abc", "http://h"),
    ],
)
def test_text_after_an_authorization_value_is_not_shown(text, expected):
    assert redact_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # The text cannot tell a prose colon after a URL from an assignment,
        # and a name in the host position goes with the URI rewrite too.
        "Request failed for https://host/password: 404 Not Found",
        "Error accessing https://host/token: connection refused",
        "{'https://host/token': 'Connection refused'}",
        "GET https://h/oauth/bearer returned 404",
        "Client error for url 'https://h/token='",
        "GET https://h/password={id} returned 404",
        "GET https://h/password:[id] returned 404",
        "GET https://h/v1/credential:[id] returned 404",
        "GET https://h/authorization=Basic returned 404",
        "see https://h/token= here",
        "Error accessing https://token: connection refused",
        "1https://password : HUNTER2",
        "+https://secret = HUNTER2",
        "1https://Bearer HUNTER2",
        "123://password : HUNTER2",
        "+++://secret = HUNTER2",
        ".-1://Bearer HUNTER2",
    ],
)
def test_a_url_ending_in_a_name_before_text_is_withheld(text):
    assert redact_text(text) == WITHHELD


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("fetch http://h/password=QXZQ", "fetch http://h"),
        ("see http://h/password reset page", "see http://h reset page"),
        ("see http://h/token. = not an assignment", "see http://h = not an assignment"),
        ("see http://[::1]:8080/token here", "see http://[::1]:8080 here"),
        ("see [docs](https://h/token) here", "see [docs](https://h here"),
        ("https://user:secret@host/input?token=", "https://host"),
        # Decoded, the URI would end at the space, but the rewrite takes all of it.
        ("http://h/password%20:QXZQ", "http://h"),
        (
            "Client error for url 'https://host/mcp?token=abc'",
            "Client error for url 'https://host'",
        ),
    ],
)
def test_a_name_whose_value_stays_inside_the_uri_is_scrubbed(text, expected):
    assert redact_text(text) == expected


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        # Documented limits: a plain value that starts inside the URI and goes
        # on past a character a URI cannot hold.
        ('got http://h/x?token=abc"def', 'got http://h"def'),
        ("http://h/token=abc<QXZQ>", "http://h<QXZQ>"),
        ("http://h/token=abc\tQXZQ", "http://h\tQXZQ"),
        # An input placeholder is shown changed, and `>` ends the URI.
        ("http://h/token=abc<redacted>QXZQ", "http://h>QXZQ"),
        # An escape joined to a name only by decoding the whole text, an
        # encoded name before the scheme, and a repr literal read back (#246).
        ("http://h/password %3Aq9z://host", "http://h %3aq9z://host"),
        ("%70assword://q9z", "%70assword://q9z"),
        ("'http://h/password\\t':q9z", "'http://h':q9z"),
    ],
)
def test_a_value_continuing_past_a_uri_is_not_caught(text, shown):
    assert redact_text(text) == shown

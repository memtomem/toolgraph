"""Redaction of credentials in error text and of secrets in endpoint labels."""

from __future__ import annotations

from toolgraph.crawler.crawl import spec_label
from toolgraph.models import ServerSpec
from toolgraph.redaction import endpoint_label, persisted_endpoint, redact_text


def test_endpoint_labels_never_include_args_or_url_secrets():
    stdio = ServerSpec(
        command="/usr/local/bin/python", args=["server.py", "--token=super-secret"]
    )
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

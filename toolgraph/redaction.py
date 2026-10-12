"""Stable, credential-free labels for persisted endpoints and diagnostics."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath
import re
import shlex
from urllib.parse import urlsplit

from toolgraph.models import ServerSpec

_URI = re.compile(r"[a-z][a-z0-9+.-]*://[^\s'\"<>]+", re.IGNORECASE)
_AUTHORIZATION = re.compile(
    r"(?i)\bauthorization['\"]?\s*[:=]\s*['\"]?"
    r"(?:(?:bearer|basic|token)\s+)?[^\s,'\"}\]]+"
)
_CREDENTIAL = re.compile(
    r"(?i)(api[-_]?key|access[-_]?token|token|password|secret)"
    r"(['\"]?\s*[:=]\s*['\"]?)([^\s,}\]]+)"
)


def _url_origin(value: str, fallback: str) -> str:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        if parsed.scheme.lower() not in {"http", "https"} or not hostname:
            return fallback
        host = f"[{hostname}]" if ":" in hostname else hostname
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme.lower()}://{host}{port}"
    except ValueError:
        return fallback


def _diagnostic_uri_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        if not parsed.scheme or not hostname:
            return "<redacted-uri>"
        host = f"[{hostname}]" if ":" in hostname else hostname
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme.lower()}://{host}{port}"
    except ValueError:
        return "<redacted-uri>"


def _executable_name(value: str | None) -> str:
    if not value:
        return "unknown"
    try:
        token = shlex.split(value)[0]
    except (ValueError, IndexError):
        return "unknown"
    posix_name = Path(token).name
    return PureWindowsPath(posix_name).name or "unknown"


def endpoint_label(spec: ServerSpec) -> str:
    """Return an endpoint label that never contains arguments or URL secrets."""
    if spec.transport == "stdio":
        return f"stdio:{_executable_name(spec.command)}"
    return _url_origin(spec.url or "", f"{spec.transport}:<redacted>")


def persisted_endpoint(transport: str, value: str | None) -> str:
    """Enforce the persistence invariant even for programmatic crawl results."""
    if transport == "stdio":
        raw = value or "unknown"
        if raw.startswith("stdio:"):
            raw = raw.removeprefix("stdio:")
        return f"stdio:{_executable_name(raw)}"
    return _url_origin(value or "", f"{transport}:<redacted>")


_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+")
# Same keywords as _CREDENTIAL, but a ':' followed by another ':' is the tool
# key separator (`token::refresh`, `vault-secret::read_file`), not an
# assignment. Used only where a match refuses the value outright.
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)(api[-_]?key|access[-_]?token|token|password|secret)"
    r"['\"]?\s*(?:=|:(?!:))"
)


def contains_credential(text: str) -> bool:
    """True when ``text`` holds an assignment- or header-shaped credential.

    For values that must be refused rather than scrubbed, because rewriting
    them would change what they identify. Covers the forms ``redact_text``
    scrubs (``password=…``, ``Authorization: …``, ``Bearer …``) without
    mistaking a ``server::tool`` separator for an assignment.
    """
    return bool(
        _CREDENTIAL_ASSIGNMENT.search(text)
        or _AUTHORIZATION.search(text)
        or _BEARER.search(text)
    )


def redact_text(value: object) -> str:
    """Scrub URLs and common credential-shaped values from an error message."""
    text = str(value)
    text = _URI.sub(lambda match: _diagnostic_uri_origin(match.group(0)), text)
    text = _AUTHORIZATION.sub("Authorization=<redacted>", text)
    text = _BEARER.sub(lambda match: f"{match.group(1)} <redacted>", text)
    return _CREDENTIAL.sub(lambda match: f"{match.group(1)}=<redacted>", text)

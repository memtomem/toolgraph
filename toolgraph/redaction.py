"""Stable, credential-free labels for persisted endpoints and diagnostics."""

from __future__ import annotations

import re
import shlex
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urlsplit

from toolgraph.models import ServerSpec

_URI = re.compile(r"[a-z][a-z0-9+.-]*://[^\s'\"<>]+", re.IGNORECASE)
_AUTHORIZATION = re.compile(
    r"(?i)\bauthorization['\"]?\s*[:=]\s*['\"]?"
    r"(?:(?:bearer|basic|token)\s+)?[^\s,'\"}\]]+"
)
# The names every credential detector treats as a secret when assigned. One
# list, so a name refused in one place is refused in artifact identities,
# review notes and control plans alike. Scrubbing (_CREDENTIAL below) keeps the
# same names but applies them in two passes.
CREDENTIAL_KEYWORDS = r"api[-_]?key|access[-_]?token|token|password|secret|credential"
# Userinfo with a password: scheme://user:password@host. Deliberately broader
# than RFC 3986: the user part may contain '@' (an email as the username) and
# the password may contain an unencoded '?' or '#', both of which still leak a
# password. The cost is refusing the rare URL whose query holds an '@' after a
# port (https://user@host:443?x=a@b), which errs toward refusing.
CREDENTIAL_URI = re.compile(r"://[^/\s]+:[^/\s]+@")
CREDENTIAL_QUERY = re.compile(rf"[?&](?:{CREDENTIAL_KEYWORDS})=[^&#\s]+", re.IGNORECASE)

# Scrubbing errs toward hiding: unlike the refusal check below, a '::' is not
# exempt, so `token::refresh` in a diagnostic reads `token=<redacted>`. A value
# such as `password::hunter2` in free error text must not survive.
#
# Two passes, not one alternation over CREDENTIAL_KEYWORDS. A single pass lets
# a newly added name swallow the next one as its value: `credential=password
# =hunter2` would leave `=hunter2` readable. The first pass is the original
# rule unchanged; the second only replaces text that is still visible, so the
# result never shows more than the original rule alone would.
_SCRUB_VALUE = r"(['\"]?\s*[:=]\s*['\"]?)([^\s,}\]]+)"
_CREDENTIAL = re.compile(r"(?i)(api[-_]?key|access[-_]?token|token|password|secret)" + _SCRUB_VALUE)
_CREDENTIAL_ADDED = re.compile(r"(?i)(credential)" + _SCRUB_VALUE)


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
    rf"(?i)({CREDENTIAL_KEYWORDS})"
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
        _CREDENTIAL_ASSIGNMENT.search(text) or _AUTHORIZATION.search(text) or _BEARER.search(text)
    )


# The refusal form of the two rules above. A single ':' is also how a tool or
# agent name is versioned (`get_token:v2`, `agent-token:blue`), so here a bare
# ':' is an assignment only when whitespace or a quote follows it (`api_key: x`,
# `token: rotated weekly`); after a quoted name (`"token":123`) it always is.
# Free text cannot tell `token: abc123` from `token: rotated weekly`; both are
# refused.
_REFUSED_ASSIGNMENT = re.compile(
    rf"(?i)({CREDENTIAL_KEYWORDS}|authorization)"
    r"(?:['\"]\s*[:=]|\s*=|\s*:(?=\s|['\"]))"
)
# `Authorization:Basic dXNlcjpwYXNz`: a scheme word, then the credential.
_REFUSED_AUTHORIZATION = re.compile(r"(?i)authorization['\"]?\s*:\s*[a-z][\w-]*\s+\S")


def _inspected(text: str) -> tuple[str, ...]:
    # One percent-decoding pass, for inspection only: the value itself is
    # never rewritten. `+` stays literal, and a doubly encoded key such as
    # `%2574oken` is not decoded a second time.
    decoded = unquote(text)
    return (text,) if decoded == text else (text, decoded)


def is_credential_shaped(text: str) -> bool:
    """True when a value that is stored or emitted as given holds a credential.

    The one refusal rule for every value Toolgraph refuses rather than
    scrubs: artifact identities, graph instance ids, review notes and review
    metadata, and every control-plan key and string. Covers credential URIs
    and queries, ``name=value`` and ``name: value`` assignments,
    ``Authorization`` and ``Bearer`` values, in the raw text and after one
    percent-decoding pass. ``server::tool`` and single-colon names such as
    ``get_token:v2`` pass.
    """
    return any(
        CREDENTIAL_URI.search(form)
        or CREDENTIAL_QUERY.search(form)
        or _REFUSED_ASSIGNMENT.search(form)
        or _REFUSED_AUTHORIZATION.search(form)
        or _BEARER.search(form)
        for form in _inspected(text)
    )


def redact_text(value: object) -> str:
    """Scrub URLs and common credential-shaped values from an error message."""
    text = str(value)
    text = _URI.sub(lambda match: _diagnostic_uri_origin(match.group(0)), text)
    return redact_credentials(text)


def redact_credentials(text: str) -> str:
    """Scrub credential-shaped free text without removing URI paths.

    Artifact callers must first apply their URI evidence-redaction policy.
    """
    text = _AUTHORIZATION.sub("Authorization=<redacted>", text)
    text = _BEARER.sub(lambda match: f"{match.group(1)} <redacted>", text)
    text = _CREDENTIAL.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    return _CREDENTIAL_ADDED.sub(lambda match: f"{match.group(1)}=<redacted>", text)

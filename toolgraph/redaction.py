"""Stable, credential-free labels for persisted endpoints and diagnostics."""

from __future__ import annotations

import re
import shlex
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urlsplit

from toolgraph.models import ServerSpec

# A scheme is the whole run of scheme characters before '://'. Starting a
# match only where such a run starts keeps the scan linear (a pattern that may
# start at any letter re-scans a long word from each one), and a malformed
# prefix such as `1https://` still counts as a URI and is scrubbed.
_SCHEME = r"(?<![a-z0-9+.-])[a-z0-9+.-]+://"
_URI = re.compile(_SCHEME + r"[^\s'\"<>]+", re.IGNORECASE)
# A quote inside a URI's authority with an '@' anywhere after it is a quoted
# userinfo password (`neo4j://u:"p w"@host`) whose end cannot be found
# reliably, so everything from the start of that token on is hidden.
_QUOTED_AUTHORITY = re.compile(_SCHEME + r"[^\s/?#'\"]*['\"]", re.IGNORECASE)
_AUTHORIZATION = re.compile(
    r"(?i)\bauthorization['\"]?\s*[:=]\s*['\"]?"
    r"(?:(?:bearer|basic|token)\s+)?[^\s,'\"}\]]+"
)
# The scrubbing form: a quoted or bracketed value is hidden through the end of
# the text, as for _SCRUB_VALUE below.
_AUTHORIZATION_SCRUB = re.compile(
    r"(?i)\bauthorization['\"]?\s*[:=]\s*"
    r"(?:['\"\[{][\s\S]*|(?:(?:bearer|basic|token)\s+)?[^\s,'\"}\]]+)"
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
# port (https://user@host:443?x=a@b), which errs toward refusing. The user part
# stops at the first ':', so one attempt never re-splits the same authority
# (`[^/\s]+:` before it took seconds on a long run of `a:` with no '@').
CREDENTIAL_URI = re.compile(r"://[^/\s:]*:[^/\s]+@")
CREDENTIAL_QUERY = re.compile(rf"[?&](?:{CREDENTIAL_KEYWORDS})=[^&#\s]+", re.IGNORECASE)

# Scrubbing errs toward hiding: unlike the refusal check below, a '::' is not
# exempt, so `token::refresh` in a diagnostic reads `token=<redacted>`. A value
# such as `password::hunter2` in free error text must not survive.
#
# A value that starts with a quote or a bracket has no end that can be found
# reliably (escaped or doubled quotes, line breaks, nested structures), so it
# is hidden through the end of the text: the diagnostic loses what follows,
# and never shows part of the value.
#
# Two passes, not one alternation over CREDENTIAL_KEYWORDS. A single pass lets
# a newly added name swallow the next one as its value: `credential=password
# =hunter2` would leave `=hunter2` readable. The second pass only replaces text
# the first left visible, so the result never shows more than the first alone.
_SCRUB_VALUE = r"(['\"]?\s*[:=]\s*)(['\"\[{][\s\S]*|[^\s,}\]]+)"
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


def _uri_origin_keeping_prefix(value: str) -> str:
    # A scheme starts at its first letter. Characters before it (`1https://`)
    # stay as they are, so a credential name the URI's host position holds
    # (`1https://password : x`) is still there for credential scrubbing. A
    # scheme without a letter (`123://`) is not a URI and is left alone.
    scheme = value[: value.index("://")]
    first = next((i for i, char in enumerate(scheme) if char.isalpha()), None)
    if first is None:
        return value
    return value[:first] + _diagnostic_uri_origin(value[first:])


def _hide_quoted_userinfo(text: str) -> str:
    # One search that must end before the last '@', so the scan stays linear
    # however many quoted URIs the text holds. The hidden span starts at the
    # beginning of the whitespace-delimited token: cutting at a nested scheme
    # (`https://user:x://"pw"@host`) would leave a prefix that reads as a URI.
    match = _QUOTED_AUTHORITY.search(text, 0, text.rfind("@"))
    if match is None:
        return text
    start = match.start()
    while start and not text[start - 1].isspace():
        start -= 1
    return text[:start] + "<redacted-uri>"


WITHHELD = "details withheld (they contain credential-shaped data)"
# A placeholder written in the input is shown as `\u2039redacted...`, so it can
# never pass for one a scrubber inserted.
_INPUT_PLACEHOLDER = re.compile(r"(?i)<(?=redacted)")
# Exactly the placeholders the scrubbers insert: the credential name in any
# case, the placeholder itself verbatim.
_INSERTED = re.compile(
    rf"(?i:{CREDENTIAL_KEYWORDS}|authorization)=<redacted>|(?i:bearer) <redacted>|<redacted-uri>"
)


def redact_text(value: object) -> str:
    """Scrub URLs and common credential-shaped values from an error message.

    The scrubbers do not decode percent-encoding, so a message whose decoded
    form is credential-shaped (`?%74oken=x`, `https%3A//user:pw@host`) is
    replaced by ``WITHHELD`` before scrubbing. After scrubbing, the text
    without the placeholders the scrubbers inserted is checked with the
    refusal rule, and withheld if it is still credential-shaped. A
    placeholder the input itself carries is shown as `\u2039redacted`.
    """
    raw = str(value)
    decoded = unquote(raw)
    if decoded != raw and is_credential_shaped(decoded):
        return WITHHELD
    text = _hide_quoted_userinfo(_INPUT_PLACEHOLDER.sub("\u2039", raw))
    text = _URI.sub(lambda match: _uri_origin_keeping_prefix(match.group(0)), text)
    text = redact_credentials(text)
    if is_credential_shaped(_INSERTED.sub(" ", text)):
        return WITHHELD
    return text


def redact_credentials(text: str) -> str:
    """Scrub credential-shaped free text without removing URI paths.

    Artifact callers must first apply their URI evidence-redaction policy.
    """
    text = _AUTHORIZATION_SCRUB.sub("Authorization=<redacted>", text)
    text = _BEARER.sub(lambda match: f"{match.group(1)} <redacted>", text)
    text = _CREDENTIAL.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    return _CREDENTIAL_ADDED.sub(lambda match: f"{match.group(1)}=<redacted>", text)

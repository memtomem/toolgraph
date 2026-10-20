"""Stable, credential-free labels for persisted endpoints and diagnostics."""

from __future__ import annotations

import ast
import re
import shlex
from collections.abc import Iterator
from itertools import islice
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
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
# A quote in userinfo after a '?' or '#' (`neo4j://u:p?"w s"@host`,
# `neo4j://u?x:"p w"@host`), which _QUOTED_AUTHORITY stops before. The ':'
# before the quote keeps a URL with a quoted query in prose
# (`"https://h?q=1" a@b`) out. The user part stops at the first ':', which
# keeps one attempt from re-splitting the same authority.
_QUOTED_PASSWORD = re.compile(_SCHEME + r"[^\s/'\":]*:[^\s/'\"]*['\"]", re.IGNORECASE)
# Quoted userinfo, refused and hidden by the same two patterns, each searched
# only before the last '@'.
_QUOTED_USERINFO = (_QUOTED_AUTHORITY, _QUOTED_PASSWORD)
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
    scrubs: review notes and reviewer names, every control-plan key and
    string, and, through ``is_credential_shaped_identity``, which adds to it,
    artifact identities and graph instance ids. Covers credential URIs
    and queries, ``name=value`` and ``name: value`` assignments,
    ``Authorization`` and ``Bearer`` values, in the raw text and after one
    percent-decoding pass. A quote in a URI's userinfo with an ``@`` after it
    (``neo4j://u:"p w"@host``) is refused as a credential URI, since where the
    quoted value ends cannot be found; this also refuses a quoted URL followed
    by an address (``"https://h" mail a@b``). ``server::tool`` and
    single-colon names such as ``get_token:v2`` pass. Prose cannot bound
    userinfo whose unquoted part holds whitespace (``neo4j://u v:"p w"@h``
    reads as a URL followed by text), so only identities refuse that.
    """
    return any(
        CREDENTIAL_URI.search(form)
        or any(pattern.search(form, 0, form.rfind("@")) for pattern in _QUOTED_USERINFO)
        or CREDENTIAL_QUERY.search(form)
        or _REFUSED_ASSIGNMENT.search(form)
        or _REFUSED_AUTHORIZATION.search(form)
        or _BEARER.search(form)
        for form in _inspected(text)
    )


def is_credential_shaped_identity(text: str) -> bool:
    """True when an identity value holds a credential.

    For values that name something (agents, tool keys, run and instance
    ids), never prose. On top of ``is_credential_shaped``, any ``://`` with an
    ``@`` after it is refused: an unquoted password may hold whitespace, ``/``
    or ``)`` (``neo4j://u:p w@host``), so its end cannot be found, and an
    identity has no reason to hold a URI with userinfo at all.
    """
    return is_credential_shaped(text) or any(
        (scheme := form.find("://")) != -1 and form.find("@", scheme) != -1
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
    # Each search must end before the last '@', so the scan stays linear
    # however many quoted URIs the text holds. A quoted password is hidden
    # wherever the refusal rule refuses one. The hidden span starts at the
    # beginning of the whitespace-delimited token: cutting at a nested scheme
    # (`https://user:x://"pw"@host`) would leave a prefix that reads as a URI.
    end = text.rfind("@")
    matches = [match for pattern in _QUOTED_USERINFO if (match := pattern.search(text, 0, end))]
    if not matches:
        return text
    start = min(match.start() for match in matches)
    while start and not text[start - 1].isspace():
        start -= 1
    return text[:start] + "<redacted-uri>"


# `repr`, `ascii`, `%r`, a container's `str()` and many library errors
# (`int('…')`) print a tab or a no-break space as an escape (`\t`, `\xa0`), so
# `password<TAB>=value` no longer reads as an assignment. Such output is a
# Python literal, read back here with Python's own parser.
#
# A candidate: a quote, then escapes or characters other than that quote, a
# backslash or a newline (repr never prints a raw newline), then the same
# quote. The repeat is possessive: the two branches never overlap, so giving
# characters back could not find another match, and a backtracking repeat
# keeps state for every character (about 180 MiB for a 1 MB literal).
_LITERAL = re.compile(r"""(['"])((?:\\.|(?!\1)[^\\\n])*+)\1""")
# The escapes repr and ascii print. A candidate holding any other backslash
# sequence is not their output and never reaches the parser, which would
# warn about an invalid escape. Bytes never hold `\u` or `\U`.
_STR_ESCAPES = re.compile(r"""\\(?:[\\'"tnr]|x[0-9a-f]{2}|u[0-9a-f]{4}|U[0-9a-f]{8})""")
_BYTES_ESCAPES = re.compile(r"""\\(?:[\\'"tnr]|x[0-9a-f]{2})""")
# A text holding more read-back literals than this is withheld unread.
_MAX_LITERALS = 32


def _escaped(text: str, index: int) -> bool:
    run = 0
    while index - run > 0 and text[index - run - 1] == "\\":
        run += 1
    return run % 2 == 1


def _parsed(span: str) -> object:
    # `\U00110000` is a well-formed escape outside Unicode: a SyntaxError.
    try:
        return ast.literal_eval(span)
    except (SyntaxError, ValueError):
        return None


def _literal_values(text: str, start: int, end: int, body: str) -> Iterator[tuple[int, str]]:
    # A span is kept only if it is exactly what repr or ascii prints for the
    # value, not merely a valid literal. When a `b` precedes the quote both
    # readings are tried: `x%r` of bytes prints `xb'…'`, `b%r` of a str `b'…'`.
    if "\\" not in _STR_ESCAPES.sub("", body):
        span = text[start:end]
        value = _parsed(span)
        if isinstance(value, str) and span in (repr(value), ascii(value)):
            yield start, value
    if start and text[start - 1] == "b" and "\\" not in _BYTES_ESCAPES.sub("", body):
        span = text[start - 1 : end]
        value = _parsed(span)
        if isinstance(value, bytes) and span == repr(value):
            yield start - 1, value.decode("utf-8", "replace")


def _repr_literals(text: str) -> Iterator[list[tuple[int, int, str]]]:
    """The readings of each literal in *text* that reads back, as (start, end, value).

    Every unescaped quote may start a literal, so a prose apostrophe (`can't
    read '…'`) cannot pair with a literal's opening quote. An escaped quote
    never starts repr output; skipping it keeps the scan linear, since each
    search then ends at the next unescaped same quote. A literal right after
    an odd run of backslashes (`C:\\` followed by repr output) is therefore
    not read.
    """
    for index, char in enumerate(text):
        if char not in "'\"" or _escaped(text, index):
            continue
        match = _LITERAL.match(text, index)
        if not match or "\\" not in match[2]:
            continue
        end = match.end()
        readings = _literal_values(text, index, end, match[2])
        if read := [(start, end, value) for start, value in readings]:
            yield read


def _repr_reads_as_credential(text: str) -> bool:
    # Counted first, so a text over the limit costs one scan and no checks.
    # Each form is then built and checked on its own: holding them all would
    # copy the whole text twice per reading.
    if sum(1 for _ in islice(_repr_literals(text), _MAX_LITERALS + 1)) > _MAX_LITERALS:
        return True
    return any(
        is_credential_shaped(value) or is_credential_shaped(text[:start] + value + text[end:])
        for readings in _repr_literals(text)
        for start, end, value in readings
    )


WITHHELD = "details withheld (they contain credential-shaped data)"
PATH_WITHHELD = "<path withheld: credential-shaped>"
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
    replaced by ``WITHHELD`` before scrubbing. Nor do they read escapes: a
    message holding a str or bytes literal exactly as ``repr`` or ``ascii``
    prints it (`'password\\t=x'`) is withheld when that literal, read back,
    is credential-shaped, alone or in place in the message, and when it holds
    more than ``_MAX_LITERALS`` such literals. Both checks read the message
    as given, before anything rewrites it. After scrubbing, the text without
    the placeholders the scrubbers inserted is checked with the refusal rule,
    and withheld if it is still credential-shaped. A placeholder the input
    itself carries is shown as `\u2039redacted`.
    """
    raw = str(value)
    decoded = unquote(raw)
    if decoded != raw and is_credential_shaped(decoded):
        return WITHHELD
    if _repr_reads_as_credential(raw):
        return WITHHELD
    text = _hide_quoted_userinfo(_INPUT_PLACEHOLDER.sub("\u2039", raw))
    text = _URI.sub(lambda match: _uri_origin_keeping_prefix(match.group(0)), text)
    text = redact_credentials(text)
    if is_credential_shaped(_INSERTED.sub(" ", text)):
        return WITHHELD
    return text


def display_path(path: object) -> str:
    """*path* as text for a message, or ``PATH_WITHHELD`` if it is credential-shaped.

    A path is shown whole or not at all: scrubbing part of it would print a
    path that does not exist. Replacing only the path keeps the rest of the
    message, which ``redact_text`` over the whole line would withhold too.
    """
    text = str(path)
    return text if redact_text(text) == text else PATH_WITHHELD


def redact_credentials(text: str) -> str:
    """Scrub credential-shaped free text without removing URI paths.

    Artifact callers must first apply their URI evidence-redaction policy.
    """
    text = _AUTHORIZATION_SCRUB.sub("Authorization=<redacted>", text)
    text = _BEARER.sub(lambda match: f"{match.group(1)} <redacted>", text)
    text = _CREDENTIAL.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    return _CREDENTIAL_ADDED.sub(lambda match: f"{match.group(1)}=<redacted>", text)

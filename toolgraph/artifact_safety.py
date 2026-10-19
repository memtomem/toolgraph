"""Evidence-only scrubbing and immutable artifact identity validation."""

import re
from urllib.parse import unquote, urlsplit

from toolgraph.redaction import is_credential_shaped_identity

# A scheme starts only where a run of scheme characters starts, so a long word
# is scanned once rather than again from each letter, and it must hold a
# letter: `123://` is not a URI, but a URI after it (`123://https://...`) is.
_SCHEME = re.compile(r"(?<![A-Za-z0-9+.-])[0-9+.-]*[A-Za-z][A-Za-z0-9+.-]*://")
_URI = re.compile(_SCHEME.pattern + r"[^\s)]+")
_RUN = re.compile(r"\S+")
# Encoded quotes count: graph resource URIs pass through
# normalize_resource_uri, which writes `u:"p w"@host` as `u:%22p%20w%22@host`.
_QUOTE = re.compile(r"['\"]|%2[27]")
_HIDDEN = "<redacted-uri>"


class EvidenceUriError(ValueError):
    """Evidence holds a URI whose credentials cannot be located."""


def _hide_quoted_userinfo(text: str) -> str:
    # A quote after a scheme with an '@' after it may be a quoted userinfo
    # password (`neo4j://u:"p w"@host/r`). Its end cannot be found reliably:
    # the quoted value may hold '@', '/', ')', '?', '#', whitespace or the
    # other quote, and the same '@' may belong to a query instead. So
    # everything after the first scheme is hidden, path included; no
    # credential URI can start before it.
    scheme = _SCHEME.search(text)
    if scheme is None:
        return text
    quote = _QUOTE.search(text, scheme.end())
    if quote is None or text.rfind("@") < quote.start():
        return text
    return text[: scheme.end()] + _HIDDEN


def _check_parseable(text: str) -> str:
    # In free text an unparseable URI has no authority to strip, and where it
    # ends is unknown. Hiding it and going on would print the rest of the
    # artifact, which may hold other values that cannot be bounded, so the
    # artifact is refused instead. (A whole resource value is bounded, so
    # display_resource hides it.) The parser's own message can quote the
    # netloc, password included, so it is not chained. This runs before the
    # run rule, which could hide one of these matches.
    for match in _URI.finditer(text):
        try:
            urlsplit(_without_prefix(match.group(0)))
        except ValueError:
            raise EvidenceUriError(
                "artifact evidence holds a URI that cannot be parsed; it is not shown"
            ) from None
    return text


def _without_prefix(value: str) -> str:
    # Characters before the scheme's first letter (`1https://`) are not part
    # of the URI.
    return value[next(i for i, char in enumerate(value) if char.isalpha()) :]


def display_resource(value: str) -> str:
    """One resource value with its userinfo, query and fragment removed.

    For a value whose boundaries are known: a resource field, a resource
    interpolated into a graph path, a whitespace-free run of free text. A
    password's end can still not be found inside the value, so nothing tries
    to: an '@' after the authority (a '/', '?', '#' or quote in an unquoted
    password, or a credential URI nested in the path) hides everything after
    the scheme, and so does a value urlsplit rejects. Each check also runs on
    one percent-decoded form; the output is never decoded.
    """
    end = value.find("://")
    # An encoded scheme separator before the first raw one (`neo4j%3A%2F%2F`)
    # marks a URI the checks below would not see.
    if "://" in unquote(value if end == -1 else value[:end]):
        return _HIDDEN
    if end == -1:
        return value
    start = len(value) - len(value.lstrip())
    scheme = _SCHEME.match(value, start)
    if scheme is None or scheme.end() != end + 3:
        # `123://u:p@h`, `urn:neo4j://u:p@h`: not one URI from its start.
        return value[: end + 3] + _HIDDEN
    # Characters before the scheme's first letter (`1https://`) are kept.
    first = start + next(i for i, char in enumerate(value[start:]) if char.isalpha())
    uri = value[first:]
    hidden = value[: end + 3] + _HIDDEN
    # A quote with an '@' after it may be a quoted password holding that '@'
    # (`u:"pre@LEAK/tail"`), so the netloc's last '@' is not its end.
    quote = _QUOTE.search(uri, end + 3 - first)
    if quote is not None and uri.rfind("@") > quote.start():
        return hidden
    decoded = unquote(uri)
    if decoded.count("@") > uri.count("@"):
        return hidden
    try:
        parts = urlsplit(uri)
        if any("@" in part for form in (parts, urlsplit(decoded)) for part in form[2:]):
            return hidden
    except ValueError:
        return hidden
    # The original netloc rather than ``parts.hostname``: urlsplit lowercases
    # hostname accessors, while graph resource identity keeps the authored case.
    host = parts.netloc.rpartition("@")[2]
    return value[:first] + f"{parts.scheme}://{host}{parts.path}"


def _redact_text(text: str) -> str:
    """Free text: each URI ends at whitespace or at a ')', as graph notation
    closes a resource with one, and the text after the ')' stays text, so a
    credential name glued to it is still there for credential scrubbing.

    A URI that cannot be shown without guessing where a password ends (an '@'
    after its authority, an encoded scheme, a ')' with an '@' after it) hides
    everything from its scheme to the end of the text: whatever followed may
    be the rest of that password, or a value whose name went with it. The
    quoted rule runs first because a quoted password may hold whitespace.
    """
    text = _check_parseable(_hide_quoted_userinfo(text))
    shown, done = [], 0
    for run in _RUN.finditer(text):
        word, base = run.group(0), run.start()
        last_at = word.rfind("@")
        position = 0
        # Each URI in the run, a ')' ending one and the text after it read on.
        while True:
            scheme = _SCHEME.search(word, position)
            head = word[position:] if scheme is None else word[position : scheme.start()]
            if unquote(head).count("://") > head.count("://"):
                return "".join(shown) + text[done : base + position] + _HIDDEN
            if scheme is None:
                # A scheme with no letter (`+++://secret=x`) is left alone, so
                # a credential name in it is still there for scrubbing.
                break
            start = scheme.start()
            authority = word.index("://", start) + 3
            close = word.find(")", authority)
            end = len(word) if close == -1 else close
            value = word[start:end]
            uri = display_resource(value)
            # A path Toolgraph built shows a hidden resource in its displayed
            # form, `scheme://<redacted-uri>`, which displays as itself; only a
            # value hidden here hides the rest.
            if (uri.endswith(_HIDDEN) and uri != value) or (close != -1 and last_at > close):
                return "".join(shown) + text[done : base + start] + word[start:authority] + _HIDDEN
            shown.append(text[done : base + start] + uri)
            done, position = base + end, end
    return "".join(shown) + text[done:]


# Positions that hold one resource value, so their boundaries are known: the
# value is displayed as a whole even when it holds whitespace. `policy` is an
# id, which display_resource keeps unless it is URI-shaped. A delta row's `ref`
# is a tool ref or a resource URI, told apart by its kind.
_RESOURCE_KEYS = frozenset(
    {"resource", "resource_uri", "read_resource", "write_resource", "policy"}
)
_RESOURCE_LIST_KEYS = frozenset(
    {"resources_created", "resources_pruned", "resources_retained_crawled"}
)


def _redact_string(value: str, *, whole: bool) -> str:
    # A string that is one URI from its start (raw or decoded, whitespace
    # around it aside) is one value too, wherever it sits.
    stripped = value.strip()
    if whole or (
        not any(char.isspace() for char in stripped)
        and (_SCHEME.match(stripped) or _SCHEME.match(unquote(stripped)))
    ):
        return display_resource(value)
    return _redact_text(value)


_MAX_REDACT_DEPTH = 100


def redact(value, *, _depth: int = 0, _whole: bool = False):
    """Recursively scrub strings that may contain resource URI credentials."""
    if _depth > _MAX_REDACT_DEPTH:
        raise ValueError(f"artifact nesting exceeds the {_MAX_REDACT_DEPTH}-level redaction limit")
    if isinstance(value, dict):
        return {
            key: redact(item, _depth=_depth + 1, _whole=_holds_resource(key, value))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, _depth=_depth + 1, _whole=_whole) for item in value]
    if isinstance(value, str):
        return _redact_string(value, whole=_whole)
    return value


def _holds_resource(key, parent: dict) -> bool:
    if key in _RESOURCE_KEYS or key in _RESOURCE_LIST_KEYS:
        return True
    return key == "ref" and "resource" in (parent.get("kind"), parent.get("node_kind"))


def validate_identity(value: str) -> None:
    if is_credential_shaped_identity(value):
        raise ValueError("artifact identity contains credential-shaped data")


_EVIDENCE_FIELDS = frozenset(
    {
        "paths",
        "deny_paths",
        "current_paths",
        "evidence",
        "resource",
        "resource_uri",
        "exception_reason",
        # Reserved evidence keys across artifacts, including control-preflight
        # exposures. These resource labels are scrubbed, not opaque identities.
        "read_resource",
        "write_resource",
    }
)
_IDENTITY_FIELDS = frozenset(
    {
        "agent",
        "principal",
        "principal_id",
        "run_id",
        "candidate",
        "tool_key",
        "eligible",
        "candidates",
        "graph_instance_id",
        "instance_id",
    }
)


def safe_artifact(value, *, _depth=0):
    """Preserve opaque identities; scrub only explicitly designated evidence."""
    if _depth > _MAX_REDACT_DEPTH:
        raise ValueError("artifact nesting exceeds safety limit")
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in _EVIDENCE_FIELDS:
                result[key] = redact(item, _whole=_holds_resource(key, value))
                continue
            if key in _IDENTITY_FIELDS:
                if isinstance(item, str):
                    validate_identity(item)
                elif isinstance(item, list):
                    for identity in item:
                        if isinstance(identity, str):
                            validate_identity(identity)
            result[key] = safe_artifact(item, _depth=_depth + 1)
        return result
    if isinstance(value, list):
        return [safe_artifact(item, _depth=_depth + 1) for item in value]
    return value

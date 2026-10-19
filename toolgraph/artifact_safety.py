"""Evidence-only scrubbing and immutable artifact identity validation."""

import re
from urllib.parse import urlsplit, urlunsplit

from toolgraph.redaction import is_credential_shaped

# A scheme starts only where a run of scheme characters starts, so a long word
# is scanned once rather than again from each letter, and it must hold a
# letter: `123://` is not a URI, but a URI after it (`123://https://...`) is.
_SCHEME = re.compile(r"(?<![A-Za-z0-9+.-])[0-9+.-]*[A-Za-z][A-Za-z0-9+.-]*://")
_URI = re.compile(_SCHEME.pattern + r"[^\s)]+")
# Encoded quotes count: graph resource URIs pass through
# normalize_resource_uri, which writes `u:"p w"@host` as `u:%22p%20w%22@host`.
_QUOTE = re.compile(r"['\"]|%2[27]")


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
    return text[: scheme.end()] + "<redacted-uri>"


def _redact_uri_keeping_prefix(value: str) -> str:
    # Characters before the scheme's first letter (`1https://`) are not part
    # of the URI and stay as they are.
    first = next(i for i, char in enumerate(value) if char.isalpha())
    return value[:first] + _redact_uri(value[first:])


def _redact_uri(value: str) -> str:
    """Remove URI userinfo and query strings without changing ordinary text."""
    if "://" not in value:
        return value
    parts = urlsplit(value)
    # Use the original netloc rather than ``parts.hostname``: urlsplit
    # lowercases hostname accessors, while graph resource identity preserves
    # the authored case. Only userinfo and query data are sensitive here.
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


_MAX_REDACT_DEPTH = 100


def redact(value, *, _depth: int = 0):
    """Recursively scrub strings that may contain resource URI credentials."""
    if _depth > _MAX_REDACT_DEPTH:
        raise ValueError(f"artifact nesting exceeds the {_MAX_REDACT_DEPTH}-level redaction limit")
    if isinstance(value, dict):
        return {key: redact(item, _depth=_depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        # Evidence paths contain a URI inside surrounding graph notation.
        return _URI.sub(
            lambda match: _redact_uri_keeping_prefix(match.group(0)),
            _hide_quoted_userinfo(value),
        )
    return value


def validate_identity(value: str) -> None:
    if is_credential_shaped(value):
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
                result[key] = redact(item)
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

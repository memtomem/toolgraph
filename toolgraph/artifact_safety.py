"""Evidence-only scrubbing and immutable artifact identity validation."""

import re
from urllib.parse import urlsplit, urlunsplit

_URI = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s)]+")


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
        raise ValueError(
            f"artifact nesting exceeds the {_MAX_REDACT_DEPTH}-level redaction limit"
        )
    if isinstance(value, dict):
        return {key: redact(item, _depth=_depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        # Evidence paths contain a URI inside surrounding graph notation.
        return _URI.sub(lambda match: _redact_uri(match.group(0)), value)
    return value


_CREDENTIAL_URI = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")
_CREDENTIAL_QUERY = re.compile(
    r"[?&](?:api[_-]?key|password|secret|token)=[^&#\s]+", re.IGNORECASE
)


def validate_identity(value: str) -> None:
    if _CREDENTIAL_URI.search(value) or _CREDENTIAL_QUERY.search(value):
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

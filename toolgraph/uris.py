"""Resource-URI canonicalization, shared by the crawler, manifest, and queries.

MCP returns resource URIs as pydantic ``AnyUrl``, which normalizes (adds a
trailing slash to authority-only URLs, lowercases scheme/host, etc.). The
manifest is authored as raw strings. Both sides must canonicalize identically
or a governed resource and a crawled resource become two different graph nodes —
which would let a deny-governed resource slip past as ALLOW. Run every resource
URI through ``normalize_resource_uri`` at both write and read boundaries.
"""

from __future__ import annotations

import re

from pydantic import AnyUrl, ValidationError

# A URI scheme (RFC 3986): scheme = ALPHA *( ALPHA / DIGIT / "+" / "-" / "." ) ":"
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
_TOOL_REF_PREFIX = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*::")


def normalize_resource_uri(uri: str) -> str:
    """Canonicalize a resource URI the same way MCP's AnyUrl does.

    Non-URL strings (custom identifiers AnyUrl can't parse) are returned
    unchanged, so they still match consistently across write and read.
    """
    try:
        return str(AnyUrl(uri))
    except (ValidationError, ValueError):
        return uri


def is_resource_ref(ref: str) -> bool:
    """Distinguish a resource URI from a tool ref / policy id.

    A resource URI carries a URI scheme (``scheme:`` — covers ``file:///x``,
    ``memory:foo``, ``urn:secret``, ``file:/tmp/a``), whereas a tool ref is the
    ``server::tool`` convention or a bare name. Only a leading ``server::``
    prefix suppresses URI detection; IPv6 URI authorities such as
    ``http://[::1]/x`` also contain ``::`` and must still be treated as
    resources.
    """
    if _TOOL_REF_PREFIX.match(ref):
        return False
    return bool(_SCHEME.match(ref))

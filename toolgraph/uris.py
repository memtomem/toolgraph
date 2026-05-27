"""Resource-URI canonicalization, shared by the crawler, manifest, and queries.

MCP returns resource URIs as pydantic ``AnyUrl``, which normalizes (adds a
trailing slash to authority-only URLs, lowercases scheme/host, etc.). The
manifest is authored as raw strings. Both sides must canonicalize identically
or a governed resource and a crawled resource become two different graph nodes —
which would let a deny-governed resource slip past as ALLOW. Run every resource
URI through ``normalize_resource_uri`` at both write and read boundaries.
"""

from __future__ import annotations

from pydantic import AnyUrl, ValidationError


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
    """A governed node is a resource (URI) if it has a scheme separator."""
    return "://" in ref

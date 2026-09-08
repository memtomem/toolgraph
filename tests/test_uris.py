"""Golden-set tripwire for resource-URI canonicalization (issue #11).

Resource node identity is ``str(pydantic.AnyUrl(uri))`` — pydantic's
normalization IS the identity function for governed resources. If a pydantic
upgrade changes any of these outputs, a governed resource authored under the
old form and a crawl under the new form become two different graph nodes:
the DENY edge stays on the orphaned node and reachability reports a false
ALLOW. That failure is silent; this file is the alarm.

These are not style assertions. Do NOT "fix" a failing golden by updating
the expected value until you have confirmed (a) which pydantic change moved
it, and (b) that existing graphs either re-normalize identically at next
crawl+ingest or get a migration note. Goldens captured under pydantic 2.13.
"""

from __future__ import annotations

import pytest

from toolgraph.uris import normalize_resource_uri

GOLDEN = [
    # authority-only URL gains a trailing slash
    ("https://example.com", "https://example.com/"),
    ("https://example.com/", "https://example.com/"),
    # scheme + host lowercase; path case preserved
    ("HTTPS://EXAMPLE.COM/Path", "https://example.com/Path"),
    # default ports are dropped
    ("http://example.com:80/x", "http://example.com/x"),
    ("https://example.com:443/x", "https://example.com/x"),
    # file URIs: triple-slash form is stable; single-slash form is lifted to it
    ("file:///private/secrets.env", "file:///private/secrets.env"),
    ("file:/tmp/a", "file:///tmp/a"),
    # IPv6 authorities: bracket form stable, host lowercased, real port kept
    ("http://[::1]/x", "http://[::1]/x"),
    ("http://[2001:DB8::1]:8080/y", "http://[2001:db8::1]:8080/y"),
    # scheme-only identifiers pass through byte-identical
    ("memory:claude:toolgraph", "memory:claude:toolgraph"),
    ("urn:secret", "urn:secret"),
    ("mailto:ops@example.com", "mailto:ops@example.com"),
    # userinfo + non-default port survive
    ("postgres://user@db.internal:5432/prod", "postgres://user@db.internal:5432/prod"),
    # non-URI strings are returned unchanged (custom identifiers must still
    # match themselves across write and read)
    ("not a uri", "not a uri"),
    ("no-scheme/path", "no-scheme/path"),
]


@pytest.mark.parametrize(("raw", "expected"), GOLDEN, ids=[g[0] for g in GOLDEN])
def test_normalization_golden(raw: str, expected: str):
    assert normalize_resource_uri(raw) == expected


def test_normalization_is_idempotent():
    """write-side and read-side both normalize; the second pass must be a
    no-op or node identity would depend on how many boundaries a URI crossed."""
    for raw, _ in GOLDEN:
        once = normalize_resource_uri(raw)
        assert normalize_resource_uri(once) == once


def test_url_shaped_unparseable_uri_warns_before_verbatim_fallback():
    import pytest

    from toolgraph.uris import ResourceUriNormalizationWarning, normalize_resource_uri

    with pytest.warns(ResourceUriNormalizationWarning):
        assert normalize_resource_uri("http://[not-a-host/x") == "http://[not-a-host/x"


def test_custom_non_url_identifier_falls_through_silently():
    import warnings as _warnings

    from toolgraph.uris import normalize_resource_uri

    with _warnings.catch_warnings():
        _warnings.simplefilter("error")
        # Not URL-shaped: stays verbatim with no warning, by design.
        assert normalize_resource_uri("§custom-id§") == "§custom-id§"

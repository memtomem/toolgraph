"""Errors whose message is safe to hand back to a caller."""

from __future__ import annotations


class ContractError(ValueError):
    """A caller-fixable violation of a documented contract.

    The distinction is load-bearing at the MCP boundary. mcp 2.x masks the text
    of an unexpected exception so an internal failure cannot leak filesystem
    paths, query bodies or credential-shaped values to a client. That default is
    right, and toolgraph keeps it for everything except the errors it raises
    deliberately here.

    A ContractError message is built only from values the caller supplied and
    fixed constants -- "unknown profile 'production', expected one of [...]" --
    so forwarding it is the answer the caller needs rather than a disclosure.

    Raise this ONLY where that is true. Never use it to wrap a driver,
    filesystem, or configuration failure: those carry text nobody audited, and
    ``redact_text`` models URLs and a handful of credential labels, not
    arbitrary confidential strings.

    It subclasses ValueError so existing ``except ValueError`` callers and the
    CLI's error handling keep working unchanged.
    """

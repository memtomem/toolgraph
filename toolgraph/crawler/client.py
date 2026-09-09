"""Enumerate one MCP server's factual capabilities into a CrawlResult."""

from __future__ import annotations

from toolgraph.crawler.transports import open_session
from toolgraph.models import CrawlResult, ResourceRecord, ServerSpec, ToolRecord
from toolgraph.redaction import endpoint_label
from toolgraph.uris import normalize_resource_uri

# A crawled server controls its own cursors, so an unbounded drain loop lets a
# buggy or malicious server that keeps returning a cursor pin the crawler and
# grow memory without limit. The per-server wall-clock timeout only applies on
# the CLI path (crawl_all), not to library callers of crawl_server. Real
# catalogs are a handful of pages; hitting this cap means the server is broken.
MAX_LIST_PAGES = 1000


class PaginationLimitError(RuntimeError):
    """A server kept returning continuation cursors past MAX_LIST_PAGES."""


def _page(cursor: str | None):
    """Pagination params for a list call, or None for the first page.

    mcp 2.x replaced the ``cursor=`` keyword with a params object. The SDK
    treats "no params" and "params with a null cursor" as equivalent when
    deciding a listing is complete, so returning None here is a matter of
    sending the smaller request, not of correctness. The distinction that DOES
    decide the loop is null versus ``""`` in the RESPONSE cursor, which the
    caller below handles.
    """
    if cursor is None:
        return None
    from mcp.types import PaginatedRequestParams

    return PaginatedRequestParams(cursor=cursor)


def _annotation_fields(tool) -> dict:
    """Tool annotations as ToolRecord fields (ADR-0006).

    Annotations are optional in the protocol; absent ones stay None so the
    loader can clear node properties the server stopped sending (SET to null
    removes the property — same reconciliation lifecycle as description).
    """
    ann = getattr(tool, "annotations", None)
    if ann is None:
        return {}
    return {
        "read_only_hint": ann.read_only_hint,
        "destructive_hint": ann.destructive_hint,
        "idempotent_hint": ann.idempotent_hint,
        "open_world_hint": ann.open_world_hint,
    }


async def _list_all_tools(session) -> list[ToolRecord]:
    """Drain ``list_tools`` pages until ``next_cursor`` is exhausted.

    Without pagination, a server with more than one page silently drops
    tools on later pages — governance refs to them would then reject at
    ingest and reachability analysis would miss the tools entirely (Codex
    PR #1 review caught this).
    """
    out: list[ToolRecord] = []
    cursor: str | None = None
    for _ in range(MAX_LIST_PAGES):
        listed = await session.list_tools(params=_page(cursor))
        out.extend(
            ToolRecord(
                name=t.name,
                description=t.description,
                input_schema=t.input_schema,
                **_annotation_fields(t),
            )
            for t in listed.tools
        )
        cursor = getattr(listed, "next_cursor", None)
        if cursor is None:
            # Stop ONLY on None: an empty-string cursor is a valid opaque
            # continuation token in MCP and the SDK distinguishes "" from
            # None when sending the next request. `if not cursor` would
            # silently truncate after page 1 against such a server.
            return out
    raise PaginationLimitError(
        f"list_tools returned a continuation cursor on {MAX_LIST_PAGES}"
        " consecutive pages — refusing to drain further"
    )


async def _list_all_resources(session) -> list[ResourceRecord]:
    """Drain ``list_resources`` pages until ``next_cursor`` is exhausted."""
    out: list[ResourceRecord] = []
    cursor: str | None = None
    for _ in range(MAX_LIST_PAGES):
        listed = await session.list_resources(params=_page(cursor))
        out.extend(
            ResourceRecord(
                uri=normalize_resource_uri(str(r.uri)),
                name=r.name,
                mime_type=r.mime_type,
                description=r.description,
            )
            for r in listed.resources
        )
        cursor = getattr(listed, "next_cursor", None)
        if cursor is None:
            # Stop ONLY on None: an empty-string cursor is a valid opaque
            # continuation token in MCP and the SDK distinguishes "" from
            # None when sending the next request. `if not cursor` would
            # silently truncate after page 1 against such a server.
            return out
    raise PaginationLimitError(
        f"list_resources returned a continuation cursor on {MAX_LIST_PAGES}"
        " consecutive pages — refusing to drain further"
    )


async def crawl_server(spec: ServerSpec) -> CrawlResult:
    """Connect, initialize, and list tools/resources the server actually advertises."""
    async with open_session(spec) as session:
        init = await session.initialize()
        caps = init.capabilities

        tools = await _list_all_tools(session) if caps.tools is not None else []
        resources = (
            await _list_all_resources(session) if caps.resources is not None else []
        )

        return CrawlResult(
            server_name=spec.name or init.server_info.name,
            server_version=init.server_info.version,
            transport=spec.transport,
            endpoint=endpoint_label(spec),
            tools=tools,
            resources=resources,
        )

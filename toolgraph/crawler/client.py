"""Enumerate one MCP server's factual capabilities into a CrawlResult."""

from __future__ import annotations

from toolgraph.crawler.transports import open_session
from toolgraph.models import CrawlResult, ResourceRecord, ServerSpec, ToolRecord
from toolgraph.uris import normalize_resource_uri


def _endpoint(spec: ServerSpec) -> str | None:
    if spec.command:
        return " ".join([spec.command, *spec.args])
    return spec.url


async def _list_all_tools(session) -> list[ToolRecord]:
    """Drain ``list_tools`` pages until ``nextCursor`` is exhausted.

    Without pagination, a server with more than one page silently drops
    tools on later pages — governance refs to them would then reject at
    ingest and reachability analysis would miss the tools entirely (Codex
    PR #1 review caught this).
    """
    out: list[ToolRecord] = []
    cursor: str | None = None
    while True:
        listed = await session.list_tools(cursor=cursor)
        out.extend(
            ToolRecord(name=t.name, description=t.description, input_schema=t.inputSchema)
            for t in listed.tools
        )
        cursor = getattr(listed, "nextCursor", None)
        if not cursor:
            return out


async def _list_all_resources(session) -> list[ResourceRecord]:
    """Drain ``list_resources`` pages until ``nextCursor`` is exhausted."""
    out: list[ResourceRecord] = []
    cursor: str | None = None
    while True:
        listed = await session.list_resources(cursor=cursor)
        out.extend(
            ResourceRecord(
                uri=normalize_resource_uri(str(r.uri)),
                name=r.name,
                mime_type=r.mimeType,
                description=r.description,
            )
            for r in listed.resources
        )
        cursor = getattr(listed, "nextCursor", None)
        if not cursor:
            return out


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
            server_name=spec.name or init.serverInfo.name,
            server_version=init.serverInfo.version,
            transport=spec.transport,
            endpoint=_endpoint(spec),
            tools=tools,
            resources=resources,
        )

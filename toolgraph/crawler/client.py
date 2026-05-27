"""Enumerate one MCP server's factual capabilities into a CrawlResult."""

from __future__ import annotations

from toolgraph.crawler.transports import open_session
from toolgraph.models import CrawlResult, ResourceRecord, ServerSpec, ToolRecord
from toolgraph.uris import normalize_resource_uri


def _endpoint(spec: ServerSpec) -> str | None:
    if spec.command:
        return " ".join([spec.command, *spec.args])
    return spec.url


async def crawl_server(spec: ServerSpec) -> CrawlResult:
    """Connect, initialize, and list tools/resources the server actually advertises."""
    async with open_session(spec) as session:
        init = await session.initialize()
        caps = init.capabilities

        tools: list[ToolRecord] = []
        if caps.tools is not None:
            listed = await session.list_tools()
            tools = [
                ToolRecord(name=t.name, description=t.description, input_schema=t.inputSchema)
                for t in listed.tools
            ]

        resources: list[ResourceRecord] = []
        if caps.resources is not None:
            listed_res = await session.list_resources()
            resources = [
                ResourceRecord(
                    uri=normalize_resource_uri(str(r.uri)),
                    name=r.name,
                    mime_type=r.mimeType,
                    description=r.description,
                )
                for r in listed_res.resources
            ]

        return CrawlResult(
            server_name=spec.name or init.serverInfo.name,
            server_version=init.serverInfo.version,
            transport=spec.transport,
            endpoint=_endpoint(spec),
            tools=tools,
            resources=resources,
        )

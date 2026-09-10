"""Open an MCP ClientSession over the right transport for a ServerSpec.

Normalizes the transport differences the SDK exposes. Under mcp 2.x every
transport yields the same 2-tuple ``(read, write)``; the session-id getter
streamable-http used to return as a third element is gone.

SSE is the legacy HTTP transport the MCP spec superseded with streamable-http;
prefer streamable-http for anything new. The SDK still ships SSE and does not
warn on it, so the branch below stays supported and is exercised for real by
``tests/test_crawler.py::test_crawl_sse`` and by the ``sse`` leg of
``scripts/verify_mcp_floor.py``, which the ``minimum-mcp`` CI job runs at the
declared SDK floor.

Uses the preferred ``streamable_http_client``; since it takes headers via a
custom httpx client rather than a ``headers=`` kwarg, we build one with the
SDK's ``create_mcp_http_client`` when auth headers are supplied. The two
spellings mean one can stop carrying credentials without the other noticing, so
``test_crawl_forwards_headers`` drives both against a fixture that rejects
tokenless requests, and the floor script pins the streamable-http one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from toolgraph.models import ServerSpec


@asynccontextmanager
async def open_session(spec: ServerSpec) -> AsyncIterator[ClientSession]:
    async with AsyncExitStack() as stack:
        if spec.transport == "stdio":
            if not spec.command:
                raise ValueError(f"stdio server {spec.name!r} needs a 'command'")
            # Merge over the SDK's safe default env so PATH etc. survive when the
            # user supplies only secrets (e.g. API keys).
            env = {**get_default_environment(), **spec.env} if spec.env else None
            params = StdioServerParameters(command=spec.command, args=spec.args, env=env)
            read, write = await stack.enter_async_context(stdio_client(params))

        elif spec.transport == "streamable-http":
            if not spec.url:
                raise ValueError(f"streamable-http server {spec.name!r} needs a 'url'")
            http_client = None
            if spec.headers:
                http_client = await stack.enter_async_context(
                    create_mcp_http_client(headers=spec.headers)
                )
            # mcp 2.x drops the session-id getter from the returned streams.
            read, write = await stack.enter_async_context(
                streamable_http_client(spec.url, http_client=http_client)
            )

        elif spec.transport == "sse":
            if not spec.url:
                raise ValueError(f"sse server {spec.name!r} needs a 'url'")
            read, write = await stack.enter_async_context(
                sse_client(spec.url, headers=spec.headers or None)
            )

        else:  # pragma: no cover - guarded by the Literal type
            raise ValueError(f"unknown transport {spec.transport!r}")

        session = await stack.enter_async_context(ClientSession(read, write))
        yield session

#!/usr/bin/env python3
"""Exercise the MCP surface this project actually depends on, at one SDK version.

The `minimum-mcp` CI job installs the declared floor and needs to prove the
floor SPEAKS the protocol, not merely that the package imports. A crawl over
stdio does not reach the pieces the 2.x migration changed most: the server's
`call_tool` override and its error envelope, a continuation cursor, and the
streamable-http transport. Those are what this script drives.

Run it against any interpreter that has toolgraph installed::

    uv run --python .minimum-venv --no-project python scripts/verify_mcp_floor.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "sample_server.py"


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'ok' if condition else 'FAIL'}] {label}{f' — {detail}' if detail else ''}")
    if not condition:
        raise SystemExit(f"floor verification failed: {label}")


async def server_boundary() -> None:
    """The override must forward a ContractError and mask anything else."""
    from mcp.types import CallToolRequestParams, CallToolResult

    from toolgraph.errors import ContractError
    from toolgraph.server import app

    entry = app.mcp._lowlevel_server.get_request_handler("tools/call")
    check("tools/call handler is registered", entry is not None)

    async def call(name, arguments):
        result = await entry.handler(None, CallToolRequestParams(
            name=name, arguments=arguments))
        assert isinstance(result, CallToolResult), type(result)
        return result

    original = app._with_generation
    try:
        app._with_generation = lambda _fetch: (_ for _ in ()).throw(
            ContractError("unknown profile 'floor-probe'"))
        forwarded = await call("eligible_tools", {"agent": "a", "candidates": []})
        check("ContractError reaches the caller", forwarded.is_error
              and "floor-probe" in forwarded.content[0].text)

        app._with_generation = lambda _fetch: (_ for _ in ()).throw(
            RuntimeError("SENTINEL_/srv/private/db.lbug"))
        masked = await call("eligible_tools", {"agent": "a", "candidates": []})
        check("an unexpected exception stays masked", masked.is_error
              and "SENTINEL_" not in masked.content[0].text,
              masked.content[0].text[:60])
    finally:
        app._with_generation = original

    tools = await app.mcp.list_tools()
    check("annotations are readable", bool(tools)
          and any(t.annotations and t.annotations.read_only_hint for t in tools),
          f"{len(tools)} tools")


async def pagination() -> None:
    """An empty-string cursor is a continuation token, not an end marker."""
    from toolgraph.crawler import client

    class Page:
        def __init__(self, tools, next_cursor):
            self.tools, self.next_cursor = tools, next_cursor

    class Tool:
        def __init__(self, name):
            self.name, self.description, self.input_schema = name, None, {}
            self.annotations = None

    class Session:
        def __init__(self):
            self.cursors = []

        async def list_tools(self, *, params=None):
            cursor = None if params is None else params.cursor
            self.cursors.append(cursor)
            if cursor is None:
                return Page([Tool("first")], "")
            return Page([Tool("second")], None)

    session = Session()
    records = await client._list_all_tools(session)
    check('an "" cursor continues the listing', [r.name for r in records]
          == ["first", "second"], f"cursors={session.cursors}")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def streamable_http() -> None:
    """The transport the crawl over stdio never touches."""
    from toolgraph.crawler.transports import open_session
    from toolgraph.models import ServerSpec

    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(FIXTURE), "http", str(port)])
    try:
        url = f"http://127.0.0.1:{port}/mcp"
        spec = ServerSpec(name="floor", transport="streamable-http", url=url)
        deadline = time.monotonic() + 30
        while True:
            try:
                async with open_session(spec) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    check("streamable-http lists tools",
                          {t.name for t in listed.tools} == {"read_file", "write_file"})
                    return
            except Exception:
                if time.monotonic() > deadline:
                    raise
                await asyncio.sleep(0.5)
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def main() -> int:
    import mcp
    from importlib.metadata import version

    print(f"verifying the MCP surface against mcp {version('mcp')}")
    del mcp
    for name, coro in (("server boundary", server_boundary()),
                       ("pagination", pagination()),
                       ("streamable-http", streamable_http())):
        print(f"{name}:")
        await coro
    print("floor verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

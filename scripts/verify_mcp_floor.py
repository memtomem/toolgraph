#!/usr/bin/env python3
"""Exercise the MCP surface this project actually depends on, at one SDK version.

The `minimum-mcp` CI job installs the declared floor and needs to prove the
floor SPEAKS the protocol, not merely that the package imports. A crawl over
stdio does not reach the pieces the 2.x migration changed most: the server's
`call_tool` override and its error envelope, a continuation cursor, the two
HTTP transports (streamable-http and legacy SSE), and auth-header forwarding.
Those are what this script drives.

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
    """Our loop treats an empty-string cursor as a continuation, not an end.

    Scope, stated plainly: the stub below stands in for a ClientSession, so
    this exercises the params object the SDK exposes and OUR loop over it. It
    does not prove the SDK carries an empty cursor over the wire -- that needs
    a real paginating server, which the fixture cannot yet be. Treat this as a
    guard on the caller, not on the transport.
    """
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
    check('an "" cursor continues the listing',
          [r.name for r in records] == ["first", "second"])
    check("the empty cursor is sent back verbatim, not dropped",
          session.cursors == [None, ""], f"cursors={session.cursors!r}")


def _rejections(port: int) -> int:
    """Read the fixture's rejection tally; unauthenticated on purpose."""
    import urllib.request

    sys.path.insert(0, str(FIXTURE.parent))
    from sample_server import RECEIPT_PATH

    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}{RECEIPT_PATH}", timeout=5
    ) as response:
        return int(response.read())


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def http_transport(mode: str, transport: str, path: str) -> None:
    """Drive one HTTP-ish transport; the crawl over stdio never touches these.

    `mode` is the fixture server's argv verb, `transport` the ServerSpec value
    and `path` the endpoint the SDK's server mounts for it.
    """
    from toolgraph.crawler.transports import open_session
    from toolgraph.models import ServerSpec

    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(FIXTURE), mode, str(port)])
    try:
        url = f"http://127.0.0.1:{port}{path}"
        spec = ServerSpec(name="floor", transport=transport, url=url)
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                raise SystemExit(
                    f"fixture server exited early with code {proc.returncode}; "
                    "retrying against a dead process would just burn the deadline"
                )
            try:
                # The deadline must bound the attempt, not only the gap between
                # attempts: a hung initialize() would otherwise wait forever.
                async with asyncio.timeout(10):
                    async with open_session(spec) as session:
                        await session.initialize()
                        listed = await session.list_tools()
                        check(f"{transport} lists tools",
                              {t.name for t in listed.tools}
                              == {"read_file", "write_file"})
                        return
            except Exception:
                if time.monotonic() > deadline:
                    raise
                await asyncio.sleep(0.5)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


async def header_forwarding() -> None:
    """Auth headers must actually reach the server at the declared floor.

    Only streamable-http is driven here. SSE hands `headers=` straight to
    `sse_client`, but streamable-http cannot take that kwarg, so we build a
    client with `create_mcp_http_client(headers=...)` and pass `http_client=` --
    a 2.x-specific spelling, and the one that can silently stop carrying
    credentials. `tests/test_crawler.py` covers both transports; this leg is
    here so the floor cannot regress it unnoticed.
    """
    sys.path.insert(0, str(FIXTURE.parent))
    from sample_server import AUTH_HEADER, AUTH_TOKEN

    from toolgraph.crawler.transports import open_session
    from toolgraph.models import ServerSpec

    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(FIXTURE), "http-auth", str(port)])
    try:
        url = f"http://127.0.0.1:{port}/mcp"
        authorized = ServerSpec(name="floor", transport="streamable-http", url=url,
                                headers={AUTH_HEADER: AUTH_TOKEN})
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                raise SystemExit(
                    f"fixture server exited early with code {proc.returncode}; "
                    "retrying against a dead process would just burn the deadline"
                )
            try:
                async with asyncio.timeout(10):
                    async with open_session(authorized) as session:
                        await session.initialize()
                        listed = await session.list_tools()
                        check("headers reach the server",
                              {t.name for t in listed.tools}
                              == {"read_file", "write_file"})
                        break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                await asyncio.sleep(0.5)

        # Without the header the same URL must be refused -- otherwise the
        # check above would pass even if the header were dropped entirely.
        #
        # The evidence is the SERVER's rejection tally, not the exception the
        # client sees. "Some exception was raised" would be vacuous: a timeout
        # or a dead port would satisfy it, and the SDK maps both a 401 and a
        # 500 onto the same generic error for streamable-http, so the exception
        # cannot distinguish "refused" from "fell over".
        anonymous = ServerSpec(name="floor", transport="streamable-http", url=url)
        before = _rejections(port)
        # The authorized loop above retries, so pin that none of its attempts
        # were turned away. Without this the comparison below could start from
        # a non-zero baseline and still look like a clean refusal.
        check("authorized requests were never rejected", before == 0,
              f"rejections so far: {before}")
        crawled_anonymously = False
        try:
            async with asyncio.timeout(10):
                async with open_session(anonymous) as session:
                    await session.initialize()
                    crawled_anonymously = True
        except Exception:
            pass
        after = _rejections(port)
        check("an unauthenticated crawl does not succeed", not crawled_anonymously)
        check("the server actually refused it", after > before,
              f"rejections {before} -> {after}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


async def main() -> int:
    import mcp
    from importlib.metadata import version

    print(f"verifying the MCP surface against mcp {version('mcp')}")
    del mcp
    # Factories, not coroutine objects: a leg that fails raises out of the loop,
    # and eagerly-built coroutines for the later legs would then be garbage
    # collected unawaited, burying the real failure under RuntimeWarnings.
    for name, make_coro in (
        ("server boundary", server_boundary),
        ("pagination", pagination),
        ("streamable-http", lambda: http_transport("http", "streamable-http", "/mcp")),
        ("sse", lambda: http_transport("sse", "sse", "/sse")),
        ("header forwarding", header_forwarding),
    ):
        print(f"{name}:")
        await make_coro()
    print("floor verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

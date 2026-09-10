"""Phase 2: crawl a real MCP server over stdio, streamable-http and SSE."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from toolgraph.crawler.client import crawl_server
from toolgraph.graph import loader
from toolgraph.models import ServerSpec

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from sample_server import AUTH_HEADER, AUTH_TOKEN, RECEIPT_PATH  # noqa: E402

FIXTURE = str(Path(__file__).parent / "fixtures" / "sample_server.py")


@pytest.fixture
def stdio_spec() -> ServerSpec:
    return ServerSpec(name="sample", transport="stdio", command=sys.executable, args=[FIXTURE])


async def test_crawl_stdio_matches_advertised_capabilities(stdio_spec):
    result = await crawl_server(stdio_spec)

    assert result.server_name == "sample"  # spec.name overrides serverInfo
    assert sorted(t.name for t in result.tools) == ["read_file", "write_file"]
    assert [r.uri for r in result.resources] == ["file:///data/customers.csv"]

    read_tool = next(t for t in result.tools if t.name == "read_file")
    assert "path" in read_tool.input_schema["properties"]


async def test_crawl_then_load_creates_edges(graph, stdio_spec):
    result = await crawl_server(stdio_spec)
    loader.load_crawl_result(result)

    counts = loader.graph_counts()
    assert counts == {
        "mcpserver": 1,
        "tool": 2,
        "resource": 1,
        "exposes": 2,
        "provides": 1,
    }


@contextmanager
def _serve(mode: str):
    """Run the fixture server in `mode` and yield its port.

    Shared by every HTTP-ish transport so streamable-http and SSE cannot drift
    apart in how they are started, waited on, or torn down.
    """
    # Pick an ephemeral free port instead of a hardcoded one: a busy CI
    # runner with 8077 taken made this fixture flaky. The tiny window
    # between closing the probe socket and the server binding is accepted.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    proc = subprocess.Popen([sys.executable, FIXTURE, mode, str(port)])
    deadline = time.time() + 25
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"sample {mode} server exited early with code {proc.returncode}"
                )
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.3)
        else:
            raise RuntimeError(f"sample {mode} server did not start")
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Reap after the kill too, or teardown returns with the child still
            # exiting and the next test races it for a port.
            proc.kill()
            proc.wait(timeout=10)


@pytest.fixture
def http_url():
    with _serve("http") as port:
        yield f"http://127.0.0.1:{port}/mcp"


@pytest.fixture
def sse_url():
    with _serve("sse") as port:
        yield f"http://127.0.0.1:{port}/sse"


async def test_crawl_streamable_http(http_url):
    spec = ServerSpec(name="sample-http", transport="streamable-http", url=http_url)
    result = await crawl_server(spec)
    assert sorted(t.name for t in result.tools) == ["read_file", "write_file"]


async def test_crawl_sse(sse_url):
    """`transport: sse` is accepted by ServerSpec, so it has to actually crawl.

    Legacy HTTP+SSE is superseded by streamable-http, but the config surface
    still admits it and the SDK still ships it, so the branch in
    `crawler/transports.py` needs a real server behind it.
    """
    spec = ServerSpec(name="sample-sse", transport="sse", url=sse_url)
    result = await crawl_server(spec)

    assert sorted(t.name for t in result.tools) == ["read_file", "write_file"]
    assert [r.uri for r in result.resources] == ["file:///data/customers.csv"]
    assert result.transport == "sse"
    # The endpoint recorded on the result is the redacted origin, never the path.
    assert result.endpoint.startswith("http://127.0.0.1:")
    assert "/sse" not in result.endpoint


# --- header forwarding: issue #103 ---------------------------------------
#
# Both HTTP transports forward `ServerSpec.headers`, but through DIFFERENT SDK
# mechanisms: streamable-http cannot take a `headers=` kwarg, so we build a
# client with `create_mcp_http_client(headers=...)` and hand it over as
# `http_client=`, while SSE passes `headers=` straight through. One spelling can
# break without the other, and this is the path that carries crawl credentials.
#
# The fixture's `-auth` modes reject any request without the token and keep a
# tally of what they turned away. Each test drives BOTH halves against ONE
# server: the same URL must succeed with headers and be refused without them.
#
# The tally, not the exception, is the evidence. `pytest.raises` alone would be
# vacuous here -- a timeout, a dead port or a crashed server satisfies it just
# as well as a refusal -- so the test asks the server how many requests it
# actually turned away.


def _rejection_count(port: int) -> int:
    """Ask the fixture how many requests it has turned away for a bad token."""
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}{RECEIPT_PATH}", timeout=5
    ) as response:
        return int(response.read())


@pytest.mark.parametrize(
    ("mode", "transport", "path"),
    [
        ("http-auth", "streamable-http", "/mcp"),
        ("sse-auth", "sse", "/sse"),
    ],
)
async def test_crawl_forwards_headers(mode, transport, path):
    with _serve(mode) as port:
        url = f"http://127.0.0.1:{port}{path}"

        authorized = ServerSpec(
            name="sample-auth",
            transport=transport,
            url=url,
            headers={AUTH_HEADER: AUTH_TOKEN},
        )
        result = await crawl_server(authorized)
        assert sorted(t.name for t in result.tools) == ["read_file", "write_file"]
        # The authorized crawl must not have been turned away even once, or the
        # comparison below would be measuring the wrong thing.
        assert _rejection_count(port) == 0

        # Same server, same URL, headers omitted: the crawl must not succeed.
        anonymous = ServerSpec(name="sample-auth", transport=transport, url=url)
        with pytest.raises(Exception):  # noqa: B017, PT011
            await crawl_server(anonymous)

        # And it must have failed because the SERVER refused it, not because
        # something else went wrong. The SDK funnels a 401 and a 500 into the
        # same generic error for streamable-http, so only the tally can tell
        # those apart.
        assert _rejection_count(port) > 0


# --- pagination: Codex PR #1 review --------------------------------------


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = None
        self.input_schema = {}


class _FakeResource:
    def __init__(self, uri: str) -> None:
        self.uri = uri
        self.name = None
        self.mime_type = None
        self.description = None


class _FakePage:
    def __init__(self, items: list, next_cursor: str | None, attr: str) -> None:
        setattr(self, attr, items)
        self.next_cursor = next_cursor


class _PagedSession:
    """Stand-in for an MCP ClientSession that returns paged list results."""

    def __init__(self, tool_pages, resource_pages) -> None:
        self.tool_pages = tool_pages
        self.resource_pages = resource_pages
        self.tool_calls: list[str | None] = []
        self.resource_calls: list[str | None] = []

    async def list_tools(self, *, params=None) -> _FakePage:
        cursor = None if params is None else params.cursor
        self.tool_calls.append(cursor)
        idx = 0 if cursor is None else int(cursor)
        items, next_cursor = self.tool_pages[idx]
        return _FakePage(items, next_cursor, "tools")

    async def list_resources(self, *, params=None) -> _FakePage:
        cursor = None if params is None else params.cursor
        self.resource_calls.append(cursor)
        idx = 0 if cursor is None else int(cursor)
        items, next_cursor = self.resource_pages[idx]
        return _FakePage(items, next_cursor, "resources")


async def test_list_all_tools_drains_paginated_results():
    """Server returns 3 tools across 2 pages — both pages must surface."""
    from toolgraph.crawler.client import _list_all_tools

    session = _PagedSession(
        tool_pages=[
            ([_FakeTool("a"), _FakeTool("b")], "1"),  # page 0 -> next_cursor=1
            ([_FakeTool("c")], None),                  # page 1 -> done
        ],
        resource_pages=[],
    )
    tools = await _list_all_tools(session)
    assert [t.name for t in tools] == ["a", "b", "c"]
    assert session.tool_calls == [None, "1"]


class _EmptyCursorSession:
    """Server that returns ``next_cursor: ""`` as an opaque continuation token.

    Codex PR #1 re-review: the SDK distinguishes None from empty string when
    requesting the next page, so the stop predicate must be ``cursor is None``,
    not ``not cursor``. Without this, a server using empty-string cursors gets
    silently truncated after page 1.
    """

    def __init__(self) -> None:
        self.tool_calls: list[str | None] = []

    async def list_tools(self, *, params=None) -> _FakePage:
        cursor = None if params is None else params.cursor
        self.tool_calls.append(cursor)
        if cursor is None:
            return _FakePage([_FakeTool("a")], "", "tools")  # next_cursor="" (not None)
        return _FakePage([_FakeTool("b")], None, "tools")


async def test_list_all_tools_treats_empty_string_cursor_as_continuation():
    from toolgraph.crawler.client import _list_all_tools

    session = _EmptyCursorSession()
    tools = await _list_all_tools(session)
    assert [t.name for t in tools] == ["a", "b"]
    assert session.tool_calls == [None, ""]


async def test_list_all_resources_drains_paginated_results():
    from toolgraph.crawler.client import _list_all_resources

    session = _PagedSession(
        tool_pages=[],
        resource_pages=[
            ([_FakeResource("file:///a")], "1"),
            ([_FakeResource("file:///b"), _FakeResource("file:///c")], None),
        ],
    )
    resources = await _list_all_resources(session)
    assert [r.uri for r in resources] == [
        "file:///a", "file:///b", "file:///c",
    ]
    assert session.resource_calls == [None, "1"]

class _RunawayCursorSession:
    """Server that returns a fresh continuation cursor on every page."""

    def __init__(self) -> None:
        self.calls = 0

    async def list_tools(self, *, params=None) -> _FakePage:
        self.calls += 1
        return _FakePage([_FakeTool(f"t{self.calls}")], str(self.calls), "tools")

    async def list_resources(self, *, params=None) -> _FakePage:
        self.calls += 1
        return _FakePage([_FakeResource(f"file:///{self.calls}")], str(self.calls), "resources")


async def test_list_all_tools_refuses_runaway_cursor(monkeypatch):
    """A server that never exhausts its cursor must fail, not loop forever."""
    from toolgraph.crawler import client

    monkeypatch.setattr(client, "MAX_LIST_PAGES", 5)
    session = _RunawayCursorSession()
    with pytest.raises(client.PaginationLimitError, match="continuation cursor"):
        await client._list_all_tools(session)
    assert session.calls == 5


async def test_list_all_resources_refuses_runaway_cursor(monkeypatch):
    from toolgraph.crawler import client

    monkeypatch.setattr(client, "MAX_LIST_PAGES", 5)
    session = _RunawayCursorSession()
    with pytest.raises(client.PaginationLimitError, match="continuation cursor"):
        await client._list_all_resources(session)
    assert session.calls == 5

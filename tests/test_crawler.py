"""Phase 2: crawl a real MCP server over stdio, streamable-http and SSE."""

from __future__ import annotations

import asyncio
import math
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from toolgraph.crawler import crawl as crawl_mod
from toolgraph.crawler.client import crawl_server
from toolgraph.graph import loader
from toolgraph.models import CrawlResult, ServersConfig, ServerSpec

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from pydantic import ValidationError
from sample_server import AUTH_HEADER, AUTH_TOKEN, RECEIPT_PATH

from toolgraph.crawler.crawl import crawl_all, crawl_all_async, load_servers_config

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
                raise RuntimeError(f"sample {mode} server exited early with code {proc.returncode}")
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
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{RECEIPT_PATH}", timeout=5) as response:
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
        with pytest.raises(Exception):  # noqa: B017
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
            ([_FakeTool("c")], None),  # page 1 -> done
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
        "file:///a",
        "file:///b",
        "file:///c",
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


_NOT_A_FLEET = {
    "blank": "",
    "whitespace": "   \n\n",
    "comment-only": "# retired everything?\n",
    "empty-mapping": "{}\n",
    "missing-key": "other: []\n",
    "null-list": "servers: null\n",
    "mapping-not-list": "servers: {}\n",
    "scalar": "servers: nope\n",
    "yaml-set": "servers: !!set {}\n",
}


@pytest.mark.parametrize("content", list(_NOT_A_FLEET.values()), ids=list(_NOT_A_FLEET))
def test_servers_yaml_requires_an_explicit_servers_list(tmp_path, content):
    """ADR-0014: only ``servers: []`` declares an empty fleet.

    The YAML set case is the one a merely-required field would miss: lax
    validation coerces ``set()`` to ``[]``; ``strict=True`` does not.
    """
    path = tmp_path / "servers.yaml"
    path.write_text(content)
    with pytest.raises(ValidationError) as info:
        load_servers_config(path)
    assert "servers" in str(info.value)


def test_servers_config_model_requires_the_key_and_keeps_explicit_empty():
    with pytest.raises(ValidationError, match="servers"):
        ServersConfig.model_validate({})
    assert ServersConfig(servers=[]).servers == []
    assert ServersConfig.model_validate({"servers": []}).servers == []


def test_servers_yaml_rejects_unknown_field(tmp_path):
    path = tmp_path / "servers.yaml"
    path.write_text("servers:\n  - command: python\n    argz: [server.py]\n")

    with pytest.raises(ValidationError, match="servers.0.argz"):
        load_servers_config(path)


def test_crawl_timeout_is_isolated():
    hang = ServerSpec(name="hang", transport="stdio", command="sleep", args=["30"])
    results, failures = crawl_all([hang], timeout=1.0)
    assert results == []
    assert failures and "timeout" in failures[0][1]


# --- crawl_all concurrency argument (#152) -----------------------------------
#
# None of these tests start a server: ``crawl_server`` is replaced in the
# ``toolgraph.crawler.crawl`` namespace, where ``_crawl_one`` resolves it.

_SAFETY = 2.0  # outer bound so a validation regression fails instead of hanging


def _spec(name: str) -> ServerSpec:
    return ServerSpec(name=name, transport="stdio", command="unused")


def _bounded_run(monkeypatch):
    """Make the sync wrapper's ``asyncio.run`` time out instead of hanging."""
    real_run = asyncio.run
    monkeypatch.setattr(
        crawl_mod.asyncio, "run", lambda coro: real_run(asyncio.wait_for(coro, _SAFETY))
    )


_INVALID_CONCURRENCY = [
    0,
    -1,
    True,
    False,
    2.0,
    0.5,
    2.5,
    math.nan,
    math.inf,
    "5",
    None,
]


@pytest.mark.parametrize("value", _INVALID_CONCURRENCY, ids=repr)
@pytest.mark.parametrize("fleet", [[], [_spec("one")]], ids=["empty", "one"])
async def test_crawl_all_async_rejects_invalid_concurrency(monkeypatch, value, fleet):
    calls: list[str] = []

    async def fake(spec):
        calls.append(spec.name)
        return CrawlResult(server_name=spec.name)

    monkeypatch.setattr(crawl_mod, "crawl_server", fake)
    with pytest.raises(ValueError, match="concurrency must be a positive integer"):
        await asyncio.wait_for(crawl_all_async(fleet, concurrency=value), _SAFETY)
    assert calls == []


@pytest.mark.parametrize("value", _INVALID_CONCURRENCY, ids=repr)
@pytest.mark.parametrize("fleet", [[], [_spec("one")]], ids=["empty", "one"])
def test_crawl_all_rejects_invalid_concurrency(monkeypatch, value, fleet):
    calls: list[str] = []

    async def fake(spec):
        calls.append(spec.name)
        return CrawlResult(server_name=spec.name)

    monkeypatch.setattr(crawl_mod, "crawl_server", fake)
    _bounded_run(monkeypatch)
    with pytest.raises(ValueError, match="concurrency must be a positive integer"):
        crawl_all(fleet, concurrency=value)
    assert calls == []


def test_crawl_all_keeps_spec_order_when_completion_order_differs(monkeypatch):
    """All three servers are in flight; they finish c, b, a; results read a, b, c."""
    specs = [_spec(n) for n in ("a", "b", "c")]
    started: dict[str, asyncio.Event] = {}
    release: dict[str, asyncio.Event] = {}

    async def fake(spec):
        started[spec.name].set()
        await release[spec.name].wait()
        return CrawlResult(server_name=spec.name)

    monkeypatch.setattr(crawl_mod, "crawl_server", fake)

    async def run():
        for s in specs:
            started[s.name] = asyncio.Event()
            release[s.name] = asyncio.Event()
        task = asyncio.create_task(crawl_all_async(specs, concurrency=3))
        for s in specs:
            await asyncio.wait_for(started[s.name].wait(), _SAFETY)
        for s in reversed(specs):
            release[s.name].set()
            await asyncio.sleep(0)
        return await asyncio.wait_for(task, _SAFETY)

    results, failures = asyncio.run(run())
    assert failures == []
    assert [r.server_name for r in results] == ["a", "b", "c"]


@pytest.mark.parametrize(("concurrency", "expected_peak"), [(2, 2), (6, 6)])
def test_crawl_all_bounds_in_flight_servers(monkeypatch, concurrency, expected_peak):
    """Six servers held open; the observed peak equals ``concurrency``.

    The ``6 -> 6`` row proves the counter can see overlap at all, so the
    ``2 -> 2`` row is not vacuous.
    """
    specs = [_spec(f"s{i}") for i in range(6)]
    in_flight = 0
    peak = 0
    entered: list[asyncio.Event] = []
    release = None

    async def fake(spec):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        entered[int(spec.name[1:])].set()
        try:
            await release.wait()
        finally:
            in_flight -= 1
        return CrawlResult(server_name=spec.name)

    monkeypatch.setattr(crawl_mod, "crawl_server", fake)

    async def run():
        nonlocal release
        release = asyncio.Event()
        entered.extend(asyncio.Event() for _ in specs)
        task = asyncio.create_task(crawl_all_async(specs, concurrency=concurrency))
        # Wait until ``concurrency`` servers are in; let the loop settle so any
        # over-admission would have happened by now.
        for ev in entered[:concurrency]:
            await asyncio.wait_for(ev.wait(), _SAFETY)
        for _ in range(5):
            await asyncio.sleep(0)
        assert in_flight == concurrency
        assert not any(ev.is_set() for ev in entered[concurrency:])
        release.set()
        return await asyncio.wait_for(task, _SAFETY)

    results, failures = asyncio.run(run())
    assert failures == []
    assert len(results) == 6
    assert peak == expected_peak


def test_crawl_all_async_cancellation_propagates_and_queued_never_start(monkeypatch):
    specs = [_spec(n) for n in ("a", "b", "c")]
    started: list[str] = []
    cleaned: list[str] = []
    first_in = None

    async def fake(spec):
        started.append(spec.name)
        first_in.set()
        try:
            await asyncio.Event().wait()  # never set: only cancellation ends it
        finally:
            cleaned.append(spec.name)
        return CrawlResult(server_name=spec.name)

    monkeypatch.setattr(crawl_mod, "crawl_server", fake)

    async def run():
        nonlocal first_in
        first_in = asyncio.Event()
        task = asyncio.create_task(crawl_all_async(specs, concurrency=1))
        await asyncio.wait_for(first_in.wait(), _SAFETY)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, _SAFETY)
        # Give queued coroutines a chance to (wrongly) start after the cancel.
        for _ in range(5):
            await asyncio.sleep(0)
        # Assert here, before ``asyncio.run`` shuts the loop down: runner
        # shutdown cancels any task still alive, which would make a detached
        # (shielded) child look "cleaned" if we checked afterwards.
        assert started == ["a"]
        assert cleaned == ["a"]

    asyncio.run(run())


def test_crawl_all_async_partial_failure_keeps_other_results(monkeypatch):
    specs = [_spec(n) for n in ("ok1", "boom", "ok2")]

    async def fake(spec):
        if spec.name == "boom":
            raise RuntimeError("connection refused")
        return CrawlResult(server_name=spec.name)

    monkeypatch.setattr(crawl_mod, "crawl_server", fake)
    results, failures = crawl_all(specs, concurrency=2)
    assert [r.server_name for r in results] == ["ok1", "ok2"]
    assert len(failures) == 1
    failed_spec, message = failures[0]
    assert failed_spec is specs[1]
    assert message == "RuntimeError: connection refused"

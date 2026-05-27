"""Phase 2: crawl a real MCP server over stdio and streamable-http."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from toolgraph.crawler.client import crawl_server
from toolgraph.graph import loader
from toolgraph.models import ServerSpec

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


@pytest.fixture
def http_url():
    port = 8077
    proc = subprocess.Popen([sys.executable, FIXTURE, "http", str(port)])
    deadline = time.time() + 25
    try:
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.3)
        else:
            raise RuntimeError("sample http server did not start")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


async def test_crawl_streamable_http(http_url):
    spec = ServerSpec(name="sample-http", transport="streamable-http", url=http_url)
    result = await crawl_server(spec)
    assert sorted(t.name for t in result.tools) == ["read_file", "write_file"]

"""Phase 1: factual loader idempotency + schema serialization."""

from __future__ import annotations

import json

from toolgraph.graph import driver, loader
from toolgraph.models import CrawlResult, ResourceRecord, ToolRecord


def _sample() -> CrawlResult:
    return CrawlResult(
        server_name="demo-fs",
        server_version="1.2.0",
        transport="stdio",
        endpoint="npx demo-fs",
        tools=[
            ToolRecord(name="read_file", description="read", input_schema={"type": "object"}),
            ToolRecord(name="write_file"),
        ],
        resources=[ResourceRecord(uri="file:///data/customers.csv", name="customers")],
    )


def test_load_is_idempotent(graph):
    cr = _sample()
    loader.load_crawl_result(cr)
    first = loader.graph_counts()
    loader.load_crawl_result(cr)
    second = loader.graph_counts()

    assert first == second
    assert first == {"mcpserver": 1, "tool": 2, "resource": 1, "exposes": 2, "provides": 1}


def test_input_schema_roundtrips_as_json(graph):
    loader.load_crawl_result(_sample())
    with driver.session() as s:
        rec = s.run("MATCH (t:Tool {name:'read_file'}) RETURN t.input_schema AS s").single()
    assert json.loads(rec["s"]) == {"type": "object"}

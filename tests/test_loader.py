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


def test_init_schema_refuses_mismatched_backend_schema_version(graph):
    import pytest

    from toolgraph.graph import driver, schema
    from toolgraph.graph.driver import BackendConfigurationError

    schema.init_schema()  # the graph fixture wiped GraphMeta; recreate it
    with driver.session() as s:
        s.run(
            "MATCH (m:GraphMeta {id:'singleton'}) SET m.backend_schema_version = 99"
        )
    with pytest.raises(BackendConfigurationError, match="backend schema version 99"):
        schema.init_schema()
    # Restore so the fixture teardown and later assertions stay clean.
    with driver.session() as s:
        s.run(
            "MATCH (m:GraphMeta {id:'singleton'}) SET m.backend_schema_version = $v",
            v=schema.BACKEND_SCHEMA_VERSION,
        )
    schema.init_schema()


def test_reset_refuses_mismatched_backend_schema_version(graph):
    from typer.testing import CliRunner

    from toolgraph import cli
    from toolgraph.graph import driver, schema

    schema.init_schema()
    with driver.session() as s:
        s.run("CREATE (:Agent {id:'survivor'})")
        s.run("MATCH (m:GraphMeta {id:'singleton'}) SET m.backend_schema_version = 99")

    result = CliRunner().invoke(cli.app, ["reset", "--yes"])

    assert result.exit_code != 0
    # The gate must fire BEFORE anything is deleted.
    with driver.session() as s:
        assert s.run("MATCH (a:Agent) RETURN count(a) AS c").single()["c"] == 1
        s.run(
            "MATCH (m:GraphMeta {id:'singleton'}) SET m.backend_schema_version = $v",
            v=schema.BACKEND_SCHEMA_VERSION,
        )


def test_tool_key_components_must_not_contain_delimiter():
    import pytest

    from toolgraph.graph import schema
    from toolgraph.models import CrawlResult, ToolRecord

    with pytest.raises(ValueError, match="delimiter"):
        schema.tool_key("a::b", "c")
    with pytest.raises(ValueError, match="delimiter"):
        schema.tool_key("a", "b::c")
    with pytest.raises(ValueError, match="delimiter"):
        ToolRecord(name="b::c")
    with pytest.raises(ValueError, match="delimiter"):
        CrawlResult(server_name="a::b")

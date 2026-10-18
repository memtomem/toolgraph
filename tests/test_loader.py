"""Phase 1: factual loader idempotency + schema serialization."""

from __future__ import annotations

import json

import pytest

from tests._helpers import load_crawl
from toolgraph.graph import driver, loader, queries
from toolgraph.manifest.ingest import ingest_governance
from toolgraph.models import (
    AccessGrant,
    CrawlResult,
    Governance,
    Policy,
    ResourceRecord,
    ToolRecord,
)


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


def test_load_is_idempotent(backend_graph):
    cr = _sample()
    loader.load_crawl_result(cr)
    first = loader.graph_counts()
    loader.load_crawl_result(cr)
    second = loader.graph_counts()

    assert first == second
    assert first == {"mcpserver": 1, "tool": 2, "resource": 1, "exposes": 2, "provides": 1}


def test_input_schema_roundtrips_as_json(backend_graph):
    loader.load_crawl_result(_sample())
    with driver.session() as s:
        rec = s.run("MATCH (t:Tool {name:'read_file'}) RETURN t.input_schema AS s").single()
    assert json.loads(rec["s"]) == {"type": "object"}


def test_init_schema_refuses_mismatched_backend_schema_version(backend_graph):
    import pytest

    from toolgraph.graph import driver, schema
    from toolgraph.graph.driver import BackendConfigurationError

    schema.init_schema()  # the graph fixture wiped GraphMeta; recreate it
    with driver.session() as s:
        s.run("MATCH (m:GraphMeta {id:'singleton'}) SET m.backend_schema_version = 99")
    with pytest.raises(BackendConfigurationError, match="backend schema version 99"):
        schema.init_schema()
    # Restore so the fixture teardown and later assertions stay clean.
    with driver.session() as s:
        s.run(
            "MATCH (m:GraphMeta {id:'singleton'}) SET m.backend_schema_version = $v",
            v=schema.BACKEND_SCHEMA_VERSION,
        )
    schema.init_schema()


def test_reset_refuses_mismatched_backend_schema_version(backend_graph):
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


def test_loader_never_persists_raw_endpoint(backend_graph):
    loader.load_crawl_result(
        CrawlResult(
            server_name="secret-test",
            transport="streamable-http",
            endpoint="https://alice:secret@example.test/mcp?token=super-secret",
            tools=[ToolRecord(name="read")],
        )
    )

    with driver.session() as current:
        row = current.run(
            "MATCH (s:MCPServer {name:'secret-test'})-[e:EXPOSES]->() "
            "RETURN s.endpoint AS endpoint, e.evidence AS evidence"
        ).single()
    assert row["endpoint"] == "https://example.test"
    assert row["evidence"] == "crawled from https://example.test"


def test_recrawl_removes_stale_tool(backend_graph):
    load_crawl("sample", ["read_file", "write_file"])
    assert loader.graph_counts()["tool"] == 2
    load_crawl("sample", ["read_file"])  # write_file gone
    counts = loader.graph_counts()
    assert counts["tool"] == 1
    assert counts["exposes"] == 1


def test_recrawl_keeps_governed_tool_and_warns(backend_graph):
    load_crawl("sample", ["read_file", "write_file"])
    ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="deny", effect="DENY")],
            grants=[AccessGrant(agent="planner", tool="sample::write_file")],
            governed_by={"sample::write_file": ["deny"]},
        )
    )
    warnings = loader.load_crawl_result(
        CrawlResult(server_name="sample", transport="stdio", tools=[ToolRecord(name="read_file")])
    )
    assert any("write_file" in w for w in warnings)
    # governance survived the recrawl rather than being silently dropped
    res = queries.check_access("planner", "sample::write_file")
    assert res["found"] is True
    assert res["verdict"] == "DENY"


def _crawl_snapshot() -> dict:
    with driver.session() as s:
        server = s.run(
            "MATCH (s:MCPServer {name:'demo-fs'}) "
            "RETURN s.version AS version, s.endpoint AS endpoint, s.last_crawled_at AS crawled"
        ).single()
        exposes = sorted(
            (r["key"], r["evidence"])
            for r in s.run(
                "MATCH (:MCPServer {name:'demo-fs'})-[e:EXPOSES]->(t:Tool) "
                "RETURN t.key AS key, e.evidence AS evidence"
            )
        )
        provides = sorted(
            (r["uri"], r["evidence"])
            for r in s.run(
                "MATCH (:MCPServer {name:'demo-fs'})-[p:PROVIDES]->(r:Resource) "
                "RETURN r.uri AS uri, p.evidence AS evidence"
            )
        )
        resources = sorted(
            (r["uri"], r["name"], r["description"])
            for r in s.run(
                "MATCH (r:Resource) RETURN r.uri AS uri, r.name AS name, "
                "r.description AS description"
            )
        )
    return {
        "server": (server["version"], server["endpoint"], str(server["crawled"])),
        "tools": queries.exposed_tool_contracts(),
        "exposes": exposes,
        "provides": provides,
        "resources": resources,
        "counts": loader.graph_counts(),
        "state": queries.graph_state(),
    }


def test_failed_recrawl_leaves_the_previous_crawl_in_place(backend_graph, monkeypatch):
    first = _sample()
    first.tools[0].read_only_hint = True
    first.tools[1].destructive_hint = True
    first.resources.append(ResourceRecord(uri="file:///data/old.csv", name="old"))
    loader.load_crawl_result(first)
    before = _crawl_snapshot()
    # Touches every write in _merge_crawl: server properties and provenance,
    # a changed tool (schema, description, hint), a new tool, a removed tool,
    # a changed resource, a new resource and a dropped PROVIDES edge.
    second = CrawlResult(
        server_name="demo-fs",
        server_version="2.0.0",
        transport="stdio",
        endpoint="uvx demo-fs",
        tools=[
            ToolRecord(name="read_file", description="changed", read_only_hint=False),
            ToolRecord(name="list_dir"),
        ],
        resources=[
            ResourceRecord(uri="file:///data/customers.csv", name="renamed"),
            ResourceRecord(uri="file:///data/orders.csv", name="orders"),
        ],
    )
    seen_inside = {}
    original = loader.bump_generation

    def fail(tx):
        rec = tx.run("MATCH (t:Tool {server:'demo-fs'}) RETURN collect(t.name) AS names").single()
        seen_inside["tools"] = sorted(rec["names"])
        raise RuntimeError("injected after mutation")

    monkeypatch.setattr(loader, "bump_generation", fail)
    with pytest.raises(RuntimeError, match="injected"):
        loader.load_crawl_result(second)
    # The failure came after the writes, so there was something to undo.
    assert seen_inside["tools"] == ["list_dir", "read_file"]
    assert _crawl_snapshot() == before

    monkeypatch.setattr(loader, "bump_generation", original)
    loader.load_crawl_result(second)
    after = _crawl_snapshot()
    assert after["server"][:2] == ("2.0.0", "stdio:uvx")
    assert [
        (t["tool_key"], t["description"], t["input_schema"], t["read_only_hint"])
        for t in after["tools"]
    ] == [("demo-fs::list_dir", None, None, None), ("demo-fs::read_file", "changed", None, False)]
    assert {evidence for _, evidence in after["exposes"] + after["provides"]} == {
        "crawled from stdio:uvx"
    }
    assert [uri for uri, _ in after["provides"]] == [
        "file:///data/customers.csv",
        "file:///data/orders.csv",
    ]
    assert ("file:///data/customers.csv", "renamed", None) in after["resources"]
    assert after["state"].generation > before["state"].generation

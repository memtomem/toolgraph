"""Tests for manifest dry-run and full delta diff reporting."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from toolgraph import cli, config
from toolgraph.graph import driver, loader, queries, schema
from toolgraph.manifest.ingest import dry_run_ingest, ingest_governance
from toolgraph.models import (
    AccessGrant,
    CrawlResult,
    DataAccess,
    ExpectedException,
    Governance,
    Policy,
    ResourceRecord,
    ToolRecord,
)


def _seed_sample_crawl() -> None:
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[
                ToolRecord(name="read_file"),
                ToolRecord(name="write_file"),
                ToolRecord(name="exec_cmd"),
            ],
            resources=[
                ResourceRecord(uri="file:///data/customers.csv"),
                ResourceRecord(uri="file:///data/public.txt"),
            ],
        )
    )


def test_dry_run_non_existent_ladybug_db_never_creates_file(tmp_path, monkeypatch):
    non_existent_dir = tmp_path / "does_not_exist"
    non_existent_db = non_existent_dir / "test.lbug"
    monkeypatch.setattr(
        config,
        "settings",
        config.Settings(backend="ladybug", db_path=non_existent_db),
    )
    driver.close_driver()
    gov = Governance(agents=["tester"])
    with pytest.raises(driver.BackendUnavailableError, match="does not exist"):
        dry_run_ingest(gov)
    assert not non_existent_dir.exists()
    assert not non_existent_db.exists()


def test_check_backend_schema_version_read_only_and_detects_mismatch(backend_graph, monkeypatch):
    # Valid version succeeds without writes
    schema.check_backend_schema_version()

    # Mismatched version raises BackendConfigurationError
    try:
        with driver.session() as s:
            s.run(
                "MERGE (m:GraphMeta {id:'singleton'}) "
                "SET m.backend_schema_version = 999"
            )
        with pytest.raises(schema.BackendConfigurationError, match="backend schema version 999"):
            schema.check_backend_schema_version()
    finally:
        with driver.session() as s:
            s.run(
                "MATCH (m:GraphMeta {id:'singleton'}) "
                "SET m.backend_schema_version = 1"
            )


def test_dry_run_leaves_database_unmodified_and_detects_all_deltas(backend_graph):
    _seed_sample_crawl()

    # Baseline manifest applied
    base_gov = Governance(
        agents=["agent-a", "agent-b"],
        policies=[
            Policy(id="pol-1", effect="ALLOW", scope="read-only", description="Initial"),
            Policy(id="pol-2", effect="DENY", scope="restricted", description="Deny"),
        ],
        grants=[
            AccessGrant(agent="agent-a", tool="sample::read_file", granted_by="team-a"),
            AccessGrant(agent="agent-b", tool="sample::write_file", granted_by="team-b"),
        ],
        data_access=[
            DataAccess(tool="sample::read_file", resource="file:///data/customers.csv", mode="READS"),
            DataAccess(tool="sample::write_file", resource="file:///data/customers.csv", mode="WRITES"),
        ],
        governed_by={
            "file:///data/customers.csv": ["pol-2"],
            "sample::exec_cmd": ["pol-2"],
        },
        expected_exceptions=[
            ExpectedException(
                agent="agent-a",
                tool="sample::read_file",
                resource="file:///data/customers.csv",
                policy="pol-2",
                reason="Legacy exception",
            )
        ],
    )
    rep = ingest_governance(base_gov)
    assert rep.applied
    base_gen = queries.graph_generation()

    # Proposed new manifest:
    # - Agent: agent-b removed, agent-c added
    # - Policy: pol-1 modified (effect ALLOW->DENY, description changed), pol-2 deleted, pol-3 added
    # - Grant: agent-a sample::read_file granted_by changed to "team-lead", agent-b revoked, agent-c added
    # - Data access: sample::read_file READS removed, sample::exec_cmd WRITES added to file:///data/public.txt
    # - Governed by: pol-2 removed, pol-3 added to file:///data/customers.csv, sample::exec_cmd governed by pol-1
    # - Exceptions: old exception removed, new exception added for agent-c
    new_gov = Governance(
        agents=["agent-a", "agent-c"],
        policies=[
            Policy(id="pol-1", effect="DENY", scope="read-only", description="Updated policy"),
            Policy(id="pol-3", effect="DENY", scope="audit", description="New deny"),
        ],
        grants=[
            AccessGrant(agent="agent-a", tool="sample::read_file", granted_by="team-lead"),
            AccessGrant(agent="agent-c", tool="sample::read_file", granted_by="team-lead"),
            AccessGrant(agent="agent-c", tool="sample::exec_cmd", granted_by="team-lead"),
        ],
        data_access=[
            DataAccess(tool="sample::write_file", resource="file:///data/customers.csv", mode="WRITES"),
            DataAccess(tool="sample::exec_cmd", resource="file:///data/public.txt", mode="WRITES"),
        ],
        governed_by={
            "file:///data/customers.csv": ["pol-3"],
            "sample::exec_cmd": ["pol-1"],
        },
        expected_exceptions=[
            ExpectedException(
                agent="agent-c",
                tool="sample::exec_cmd",
                resource=None,
                policy="pol-1",
                reason="Direct wildcard exception",
            )
        ],
    )

    diff = dry_run_ingest(new_gov)
    assert diff.applied
    assert diff.warnings == []

    # Database must remain 100% untouched
    assert queries.graph_generation() == base_gen

    # Check diff output
    assert diff.agents_created == ["agent-c"]
    assert diff.agents_deleted == ["agent-b"]

    assert any(p["id"] == "pol-3" for p in diff.policies_created)
    assert diff.policies_deleted == ["pol-2"]
    assert any(p["id"] == "pol-1" and p["after"]["effect"] == "DENY" for p in diff.policy_properties_modified)

    assert any(g["agent"] == "agent-c" and g["tool"] == "sample::read_file" for g in diff.grants_added)
    assert any(g["agent"] == "agent-b" and g["tool"] == "sample::write_file" for g in diff.grants_revoked)
    assert any(g["agent"] == "agent-a" and g["tool"] == "sample::read_file" and g["after"] == "team-lead" for g in diff.granted_by_modified)

    assert any(da["tool"] == "sample::exec_cmd" and da["resource"] == "file:///data/public.txt" for da in diff.data_access_added)
    assert any(da["tool"] == "sample::read_file" and da["resource"] == "file:///data/customers.csv" for da in diff.data_access_removed)

    assert any(gb["policy"] == "pol-3" for gb in diff.governed_by_added)
    assert any(gb["policy"] == "pol-2" for gb in diff.governed_by_removed)

    assert any(e["agent"] == "agent-c" and e["policy"] == "pol-1" for e in diff.expected_exceptions_added)
    assert any(e["agent"] == "agent-a" and e["policy"] == "pol-2" for e in diff.expected_exceptions_removed)


def test_whole_graph_final_degree_zero_pruning_semantics(backend_graph):
    """Verify that dry-run projected final degree (ADR-0001, ADR-0007) matches real ingest."""
    # 1. Crawl server with 2 tools and 2 resources
    loader.load_crawl_result(
        CrawlResult(
            server_name="test_srv",
            tools=[ToolRecord(name="tool_keep"), ToolRecord(name="tool_orphan")],
            resources=[ResourceRecord(uri="file:///res/keep"), ResourceRecord(uri="file:///res/orphan")],
        )
    )

    # 2. Manifest grants and accesses all of them
    gov1 = Governance(
        agents=["agent1"],
        policies=[Policy(id="p1", effect="ALLOW")],
        grants=[
            AccessGrant(agent="agent1", tool="test_srv::tool_keep"),
            AccessGrant(agent="agent1", tool="test_srv::tool_orphan"),
        ],
        data_access=[
            DataAccess(tool="test_srv::tool_keep", resource="file:///res/keep", mode="READS"),
            DataAccess(tool="test_srv::tool_orphan", resource="file:///res/orphan", mode="READS"),
        ],
    )
    assert ingest_governance(gov1).applied

    # 3. Crawler updates server: tool_orphan and res_orphan are no longer exposed/provided
    # loader.py drops EXPOSES/PROVIDES, but keeps Tool and Resource nodes if they have authored edges
    loader.load_crawl_result(
        CrawlResult(
            server_name="test_srv",
            tools=[ToolRecord(name="tool_keep")],
            resources=[ResourceRecord(uri="file:///res/keep")],
        )
    )

    # 4. In new manifest, authored governance on tool_orphan and res_orphan is removed
    gov2 = Governance(
        agents=["agent1"],
        policies=[Policy(id="p1", effect="ALLOW")],
        grants=[
            AccessGrant(agent="agent1", tool="test_srv::tool_keep"),
        ],
        data_access=[
            DataAccess(tool="test_srv::tool_keep", resource="file:///res/keep", mode="READS"),
        ],
    )

    # Dry-run on gov2 should report projected final degree 0:
    # res_orphan and tool_orphan must be in resources_pruned and tools_pruned!
    diff = dry_run_ingest(gov2)
    assert "file:///res/orphan" in diff.resources_pruned
    assert "test_srv::tool_orphan" in diff.tools_pruned

    # Now run actual ingest_governance(gov2)
    assert ingest_governance(gov2).applied

    # Check that real graph deleted exactly those nodes!
    with driver.session() as s:
        res_nodes = {r["uri"] for r in s.run("MATCH (res:Resource) RETURN res.uri AS uri")}
        tool_nodes = {r["key"] for r in s.run("MATCH (t:Tool) RETURN t.key AS key")}
    assert "file:///res/orphan" not in res_nodes
    assert "file:///res/keep" in res_nodes
    assert "test_srv::tool_orphan" not in tool_nodes
    assert "test_srv::tool_keep" in tool_nodes


def test_cli_ingest_manifest_dry_run(backend_graph, tmp_path):
    _seed_sample_crawl()
    manifest_file = tmp_path / "governance.yaml"
    manifest_file.write_text(
        """
agents:
  - cli-agent
policies:
  - id: cli-deny
    effect: DENY
grants:
  - agent: cli-agent
    tool: sample::read_file
"""
    )
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["ingest-manifest", "--governance", str(manifest_file), "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "DRY-RUN: manifest at" in result.stdout
    assert "Agents: +1 / -0" in result.stdout
    assert "Policies: +1 / -0" in result.stdout
    assert "Grants: +1 / -0" in result.stdout

    # Confirm database is still empty of authored nodes
    with driver.session() as s:
        assert s.run("MATCH (a:Agent) RETURN count(a) AS c").single()["c"] == 0

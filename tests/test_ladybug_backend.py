"""Embedded Ladybug backend conformance smoke tests."""

from __future__ import annotations

import subprocess
import sys

import pytest
from typer.testing import CliRunner

from toolgraph import cli, config
from toolgraph.graph import driver, loader, queries, schema, selector
from toolgraph.manifest.ingest import ingest_governance
from toolgraph.models import (
    AccessGrant,
    CrawlResult,
    DataAccess,
    Governance,
    Policy,
    ResourceRecord,
    ToolRecord,
)
from toolgraph.policy_bundle import build_policy_bundle

SECRET = "file:///secret.txt"


@pytest.fixture
def ladybug_graph(tmp_path, monkeypatch):
    pytest.importorskip("ladybug")
    settings = config.Settings(backend="ladybug", db_path=tmp_path / "graph.lbug")
    monkeypatch.setattr(config, "settings", settings)
    driver.close_driver()
    schema.init_schema()
    yield settings
    driver.close_driver()


def _seed() -> None:
    loader.load_crawl_result(
        CrawlResult(
            server_name="alpha",
            tools=[ToolRecord(name="read"), ToolRecord(name="ping")],
            resources=[ResourceRecord(uri=SECRET)],
        )
    )
    report = ingest_governance(
        Governance(
            agents=["codex"],
            policies=[Policy(id="secret-deny", effect="DENY")],
            grants=[
                AccessGrant(agent="codex", tool="alpha::read"),
                AccessGrant(agent="codex", tool="alpha::ping"),
            ],
            data_access=[
                DataAccess(tool="alpha::read", resource=SECRET, mode="READS")
            ],
            governed_by={SECRET: ["secret-deny"]},
        )
    )
    assert report.applied, report.warnings


def test_ladybug_end_to_end_policy_bundle(ladybug_graph):
    _seed()
    assert queries.check_access("codex", "alpha::read")["verdict"] == "DENY"
    filtered = selector.eligible_tools(
        "codex", ["alpha::ping", "alpha::read"], profile="strict"
    )
    assert filtered["eligible"] == []  # ping is intentionally UNMAPPED
    assert {row["reason"] for row in filtered["rejected"]} == {
        "UNMAPPED",
        "DENY_VIOLATION",
    }
    bundle = build_policy_bundle(agent="codex", profile="strict")
    assert bundle["graph_state"]["instance_id"]
    assert {row["tool_key"] for row in bundle["tools"]} == {
        "alpha::ping",
        "alpha::read",
    }


def test_ladybug_rejected_ingest_preserves_graph_state(ladybug_graph):
    _seed()
    before = queries.graph_state()
    rejected = ingest_governance(
        Governance(
            agents=["codex"],
            grants=[AccessGrant(agent="codex", tool="alpha::missing")],
        )
    )
    assert not rejected.applied
    assert queries.graph_state() == before


def test_ladybug_operational_query_conformance(ladybug_graph):
    _seed()
    unsafe = queries.unsafe_callable_tools("codex")
    assert unsafe and unsafe[0]["classification"] == "violation"
    radius = queries.blast_radius(SECRET)
    assert radius["found"] is True and radius["impacted"]
    assert {row["tool_key"] for row in queries.unmapped_tools()} == {"alpha::ping"}
    assert queries.orphan_policies() == []
    assert queries.unbacked_edges()
    report = queries.audit_report()
    assert report["status"] == "fail"

    retired, warnings = loader.retire_unlisted_servers(set())
    assert retired == ["alpha"] and warnings
    assert {row["tool_key"] for row in queries.drifted_tools()} == {
        "alpha::ping",
        "alpha::read",
    }


def test_ladybug_database_has_one_process_owner(ladybug_graph):
    code = (
        "import ladybug as lb, sys; "
        "db=lb.Database(sys.argv[1]); conn=lb.Connection(db); "
        "conn.execute('RETURN 1 AS ok').get_all()"
    )
    attempted = subprocess.run(
        [sys.executable, "-c", code, str(ladybug_graph.db_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert attempted.returncode != 0
    assert "lock" in (attempted.stdout + attempted.stderr).lower()


def test_init_defaults_to_ladybug_and_persists_runtime_config(tmp_path, monkeypatch):
    pytest.importorskip("ladybug")
    monkeypatch.chdir(tmp_path)
    driver.close_driver()
    result = CliRunner().invoke(cli.app, ["init"])
    try:
        assert result.exit_code == 0, result.output
        saved = config.Settings()
        assert saved.backend == "ladybug"
        assert saved.db_path == tmp_path / ".toolgraph" / "toolgraph.lbug"
        assert (tmp_path / ".toolgraph" / "config.json").exists()
    finally:
        driver.close_driver()


def test_ladybug_reset_changes_instance_id_and_returns_to_generation_zero(
    ladybug_graph,
):
    _seed()
    before = queries.graph_state()
    result = CliRunner().invoke(cli.app, ["reset", "--yes"])
    assert result.exit_code == 0, result.output
    after = queries.graph_state()
    assert after.generation == 0
    assert after.instance_id and after.instance_id != before.instance_id

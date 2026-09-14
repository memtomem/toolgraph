"""Tests for toolgraph doctor and runtime health inspection."""

from __future__ import annotations

import json
import os

from typer.testing import CliRunner

from toolgraph import cli, config
from toolgraph.graph import driver, schema
from toolgraph.health import run_doctor


def test_config_health_missing_and_invalid(tmp_path, monkeypatch):
    # 1. Non-existent file
    missing = tmp_path / "missing.json"
    monkeypatch.setenv("TOOLGRAPH_CONFIG", str(missing))
    info = config.check_config_health()
    assert info["exists"] is False
    assert info["healthy"] is False

    # 2. Invalid JSON file
    broken = tmp_path / "broken.json"
    broken.write_text("{bad json")
    monkeypatch.setenv("TOOLGRAPH_CONFIG", str(broken))
    info = config.check_config_health()
    assert info["exists"] is True
    assert info["valid_json"] is False
    assert info["healthy"] is False

    # 3. Valid JSON with 0600 permissions
    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps({"schema_version": 1, "backend": "ladybug"}))
    if os.name != "nt":
        valid.chmod(0o600)
    monkeypatch.setenv("TOOLGRAPH_CONFIG", str(valid))
    info = config.check_config_health()
    assert info["exists"] is True
    assert info["valid_json"] is True
    if os.name != "nt":
        assert info["permissions_ok"] is True
    assert info["healthy"] is True


def test_inspect_backend_schema_live(backend_graph):
    schema.init_schema()
    # After schema.init_schema() (done by backend_graph fixture), inspect should report healthy
    info = schema.inspect_backend_schema()
    assert info["healthy"] is True
    assert info["version_ok"] is True
    assert info["schema_version"] == 1
    if info["backend"] == "ladybug":
        assert info["missing_tables"] == []
        assert "MCPServer" in info["live_tables"]
        assert "Tool" in info["live_tables"]
        assert "GraphMeta" in info["live_tables"]
    else:
        assert info["missing_constraints"] == []
        assert "tool_key" in info["live_constraints"]
        assert "mcpserver_name" in info["live_constraints"]


def test_inspect_backend_schema_broken_version(backend_graph):
    schema.init_schema()
    try:
        with driver.session() as s:
            s.run("MERGE (m:GraphMeta {id:'singleton'}) SET m.backend_schema_version = 42")
        info = schema.inspect_backend_schema()
        assert info["healthy"] is False
        assert info["version_ok"] is False
        assert info["schema_version"] == 42
    finally:
        with driver.session() as s:
            s.run("MATCH (m:GraphMeta {id:'singleton'}) SET m.backend_schema_version = 1")


def test_run_doctor_healthy(backend_graph):
    schema.init_schema()
    report = run_doctor()
    assert report.healthy is True
    check_names = {c.name for c in report.checks}
    assert "Connectivity" in check_names
    assert "Schema Integrity" in check_names
    assert "Graph State" in check_names


def test_cli_doctor(backend_graph):
    schema.init_schema()
    runner = CliRunner()
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "Toolgraph Doctor Diagnosis:" in result.stdout
    assert "Overall status: HEALTHY" in result.stdout

    result_json = runner.invoke(cli.app, ["doctor", "--json"])
    assert result_json.exit_code == 0, result_json.output
    parsed = json.loads(result_json.stdout)
    assert parsed["healthy"] is True
    assert len(parsed["checks"]) >= 3


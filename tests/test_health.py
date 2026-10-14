"""Tests for toolgraph doctor and runtime health inspection."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from toolgraph import cli, config
from toolgraph.graph import driver, queries, schema
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


@pytest.mark.parametrize("phase", ["inspection", "settings", "connectivity", "schema", "state"])
def test_doctor_redacts_all_exception_reports(monkeypatch, phase):
    info = {
        "path": "probe.json",
        "exists": True,
        "valid_json": True,
        "permissions_ok": True,
        "permissions": "0o600",
        "healthy": True,
        "detail": "ok",
    }
    monkeypatch.setattr(config, "check_config_health", lambda: info)
    monkeypatch.setattr(driver, "verify_connectivity", lambda **kwargs: None)
    monkeypatch.setattr(
        schema,
        "inspect_backend_schema",
        lambda: {
            "healthy": True,
            "schema_version": 1,
            "backend": "ladybug",
            "live_tables": [],
        },
    )
    from contextlib import nullcontext

    monkeypatch.setattr(driver, "acquire_readonly_session", lambda: nullcontext(None))
    monkeypatch.setattr(queries, "graph_state", lambda *_: queries.GraphState("probe", 1))
    error = RuntimeError("bolt://user:synthetic-password@localhost:7687 token=synthetic-token")
    targets = {
        "inspection": (config, "check_config_health"),
        "settings": (config, "__getattr__"),
        "connectivity": (driver, "verify_connectivity"),
        "schema": (schema, "inspect_backend_schema"),
        "state": (queries, "graph_state"),
    }
    if phase == "settings":
        monkeypatch.delattr(config, "settings", raising=False)
    with patch.object(*targets[phase], side_effect=error):
        report = run_doctor()
    assert not report.healthy
    for text in (report.summary_text(), json.dumps(report.to_dict())):
        assert "synthetic-password" not in text
        assert "synthetic-token" not in text
        assert "<redacted>" in text


def test_doctor_invalid_config_in_fresh_process(tmp_path):
    config_path = tmp_path / "broken.json"
    config_path.write_text("{invalid json")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from toolgraph.cli import app; app()",
            "doctor",
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "TOOLGRAPH_CONFIG": str(config_path)},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert report["healthy"] is False
    assert [check["name"] for check in report["checks"]] == ["Configuration"]
    assert "Traceback" not in result.stderr


def test_doctor_redacts_configuration_details_before_early_return(monkeypatch):
    monkeypatch.setattr(
        config,
        "check_config_health",
        lambda: {
            "path": "probe.json",
            "exists": True,
            "valid_json": False,
            "permissions_ok": True,
            "permissions": "0o600",
            "healthy": False,
            "detail": "could not load bolt://user:synthetic-secret@localhost:7687",
        },
    )
    with patch.object(driver, "verify_connectivity") as connect:
        report = run_doctor()
    connect.assert_not_called()
    assert not report.healthy
    assert "synthetic-secret" not in report.summary_text()
    assert "synthetic-secret" not in json.dumps(report.to_dict())


@pytest.mark.parametrize("json_output", [False, True])
def test_doctor_missing_ladybug_does_not_create_files(embedded_path, json_output):
    result = CliRunner().invoke(cli.app, ["doctor"] + (["--json"] if json_output else []))
    assert result.exit_code == 1, result.output
    assert not embedded_path.parent.exists()
    assert "Traceback" not in result.output
    if json_output:
        report = json.loads(result.stdout)
        assert not report["healthy"]
        connectivity = next(c for c in report["checks"] if c["name"] == "Connectivity")
        assert connectivity["status"] == "FAIL"
        assert "does not exist" in connectivity["message"]
    else:
        assert "does not exist" in result.stdout

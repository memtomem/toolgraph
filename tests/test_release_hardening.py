"""Release boundary tests: fail-closed input and secret-free diagnostics."""

from __future__ import annotations

import json
import sys

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner
import yaml

from toolgraph.cli import app
from toolgraph import __version__, config
from toolgraph.crawler.crawl import load_servers_config, spec_label
from toolgraph.graph import driver, loader
from toolgraph.manifest.parser import load_governance
from toolgraph.models import CrawlResult, ServerSpec, ToolRecord
from toolgraph.redaction import endpoint_label, persisted_endpoint, redact_text


def test_servers_yaml_rejects_unknown_field(tmp_path):
    path = tmp_path / "servers.yaml"
    path.write_text("servers:\n  - command: python\n    argz: [server.py]\n")

    with pytest.raises(ValidationError, match="servers.0.argz"):
        load_servers_config(path)


@pytest.mark.parametrize(
    "content",
    [
        "governed_by: {}\ngoverned_by: {}\n",
        "policies:\n  - id: one\n    id: two\n    effect: DENY\n",
    ],
)
def test_governance_yaml_rejects_duplicate_keys_at_any_depth(tmp_path, content):
    path = tmp_path / "governance.yaml"
    path.write_text(content)

    with pytest.raises(yaml.constructor.ConstructorError, match="duplicate key"):
        load_governance(path)


def test_governance_yaml_rejects_unknown_nested_field(tmp_path):
    path = tmp_path / "governance.yaml"
    path.write_text(
        "policies:\n  - id: deny\n    effect: DENY\n    descriptino: typo\n"
    )

    with pytest.raises(ValidationError, match="policies.0.descriptino"):
        load_governance(path)


def test_invalid_governance_cli_is_concise_and_does_not_mutate_graph(graph, tmp_path):
    path = tmp_path / "governance.yaml"
    path.write_text("agents: [existing]\nagents: [replacement]\n")
    with driver.session() as current:
        before = current.run("MATCH (n) RETURN count(n) AS count").single()["count"]

    result = CliRunner().invoke(
        app, ["ingest-manifest", "--governance", str(path)]
    )

    assert result.exit_code == 1
    assert "Invalid governance config" in result.output
    assert "duplicate key 'agents'" in result.output
    assert "Traceback" not in result.output
    with driver.session() as current:
        after = current.run("MATCH (n) RETURN count(n) AS count").single()["count"]
    assert after == before


def test_endpoint_labels_never_include_args_or_url_secrets():
    stdio = ServerSpec(
        command="/usr/local/bin/python", args=["server.py", "--token=super-secret"]
    )
    http = ServerSpec(
        transport="streamable-http",
        url="https://alice:secret@example.test:8443/mcp?token=super-secret#fragment",
    )

    assert endpoint_label(stdio) == "stdio:python"
    assert spec_label(stdio) == "stdio:python"
    assert endpoint_label(http) == "https://example.test:8443"
    assert persisted_endpoint("stdio", "python server.py --token=secret") == "stdio:python"


def test_loader_never_persists_raw_endpoint(graph):
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


def test_error_redaction_removes_urls_and_credential_values():
    message = (
        "failed https://alice:secret@example.test/mcp?token=abc "
        "Authorization: Bearer sk-abc123 password=hunter2 "
        "mcp://bob:pw@mcp.example.test/tools?token=def"
    )

    redacted = redact_text(message)
    assert "alice" not in redacted
    assert "secret" not in redacted.lower().replace("<redacted>", "")
    assert "abc" not in redacted
    assert "sk-abc123" not in redacted
    assert "hunter2" not in redacted
    assert "https://example.test" in redacted
    assert "bob" not in redacted
    assert "def" not in redacted
    assert "mcp://mcp.example.test" in redacted


def test_explicit_config_beats_environment_backend(monkeypatch, tmp_path):
    runtime = tmp_path / "config.json"
    db_path = tmp_path / "isolated.lbug"
    runtime.write_text(
        json.dumps({"schema_version": 1, "backend": "ladybug", "db_path": str(db_path)})
    )
    monkeypatch.setenv("TOOLGRAPH_BACKEND", "neo4j")
    monkeypatch.setenv("TOOLGRAPH_DB_PATH", str(tmp_path / "shared.lbug"))
    monkeypatch.setattr(config, "_explicit_config_path", None)
    monkeypatch.setattr(config, "settings", config.settings)

    result = CliRunner().invoke(app, ["--config", str(runtime), "check"])

    assert result.exit_code == 0, result.output
    assert "backend=ladybug" in result.output
    assert str(db_path) in result.output


def test_example_init_creates_self_contained_assets_and_refuses_collision(tmp_path):
    destination = tmp_path / "quickstart"
    runner = CliRunner()

    created = runner.invoke(app, ["example", "init", str(destination)])
    assert created.exit_code == 0, created.output
    assert sorted(path.name for path in destination.iterdir()) == [
        "governance.yaml",
        "policy_gateway_server.py",
        "servers.yaml",
    ]
    servers = (destination / "servers.yaml").read_text()
    assert sys.executable in servers
    assert "__TOOLGRAPH_PYTHON__" not in servers
    assert f"Next: cd {destination} && toolgraph init" in created.output

    collision = runner.invoke(app, ["example", "init", str(destination)])
    assert collision.exit_code != 0
    assert "already exists" in collision.output


def test_version_flag_uses_package_version():
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"toolgraph {__version__}"

"""Release boundary tests: fail-closed input and secret-free diagnostics."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

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


ROOT = Path(__file__).resolve().parents[1]


def test_dev_dependency_group_is_not_passed_as_an_extra():
    workflow = (
        ROOT / ".github/workflows/policy-bundle-gateway-smoke.yml"
    ).read_text()
    operational_guide = (ROOT / "docs/gate-e-p4-operational.md").read_text()
    makefile = (ROOT / "Makefile").read_text()

    assert workflow.count("--group dev") == 2
    assert "--extra dev" not in workflow
    assert "--group dev" in operational_guide
    assert "--extra dev" not in operational_guide
    assert makefile.count("--group dev") == 2
    assert "--extra dev" not in makefile


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


def _release_workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())


# Actions the publish job is allowed to run. It is an allowlist because the
# question is not "did someone add a checkout" but "what runs in the job that
# can publish as this project" -- anything not named here has to be argued for.
PUBLISH_ALLOWED_ACTIONS = {
    "actions/download-artifact",
    "pypa/gh-action-pypi-publish",
}


def test_publishing_authority_is_isolated_from_build_code():
    """Nothing but the uploader may run in the job that can publish.

    This is the property that makes Trusted Publishing safe to enable: a
    compromised build script or transitive build dependency must not execute
    inside a job that can mint an OIDC token and upload as this project. It is
    invisible in a diff -- one added `run:` step looks harmless -- so the shape
    of the job is pinned here rather than described in a comment.

    Note what is *not* claimed: `actions/download-artifact` and the PyPI
    publisher are themselves code, pinned by commit. The claim is that no
    checked-out project code and no installed dependency runs here.
    """
    workflow = _release_workflow()
    jobs = workflow["jobs"]

    # Workflow-level permissions are inherited, so a job with no `permissions:`
    # block is not thereby unprivileged.
    assert "id-token" not in (workflow.get("permissions") or {})
    privileged = [
        name
        for name, job in jobs.items()
        if (job.get("permissions") or {}).get("id-token") == "write"
    ]
    assert privileged == ["publish"]

    publish = jobs["publish"]
    assert publish.get("needs") == "build" or "build" in publish.get("needs", [])
    assert (publish.get("environment") or {}).get("name") == "pypi"

    run_steps = []
    for step in publish["steps"]:
        assert not step.get("continue-on-error"), (
            "a step in the publish job may not be allowed to fail"
        )
        if "uses" in step:
            action = step["uses"].split("@")[0]
            assert action in PUBLISH_ALLOWED_ACTIONS, (
                f"{action} would run in the job that can publish"
            )
            assert "@" in step["uses"] and len(step["uses"].split("@")[1]) == 40, (
                "actions in the publish job must be pinned to a full commit sha"
            )
        if "run" in step:
            run_steps.append(step)

    # One shell step is tolerated: the digest check. It must not interpolate
    # anything -- `${{ }}` inside `run:` is textual substitution, so a value a
    # compromised build controls would become shell code executing right here.
    assert len(run_steps) == 1, "the publish job should run one shell step"
    assert "${{" not in run_steps[0]["run"], (
        "the publish job must not interpolate expressions into a run: block; "
        "pass values through env: instead"
    )
    for forbidden in ("uv ", "pip ", "python ", "scripts/"):
        assert forbidden not in run_steps[0]["run"], (
            f"the publish job must not invoke {forbidden.strip()}"
        )

    build = jobs["build"]
    assert "id-token" not in (build.get("permissions") or {})
    assert any(
        "scripts/verify-artifacts.sh" in step.get("run", "")
        for step in build["steps"]
    )


def test_production_publish_does_not_skip_existing_versions():
    """A production version that already exists is a failure, not a no-op.

    `skip-existing` is right for the TestPyPI rehearsal, which gets re-run. On
    PyPI it would turn a duplicate or partial publication into a green check.

    Keyed on each step's `if:` condition rather than its display name, so
    renaming a step or adding a second production publisher cannot slip past.
    """
    steps = _release_workflow()["jobs"]["publish"]["steps"]
    publishers = [
        step
        for step in steps
        if step.get("uses", "").startswith("pypa/gh-action-pypi-publish@")
    ]
    assert len(publishers) == 2

    production = [
        step
        for step in publishers
        if "!startsWith(github.ref_name, 'test-')" in step.get("if", "")
    ]
    rehearsal = [
        step
        for step in publishers
        if step.get("if", "").strip().startswith("${{ startsWith(github.ref_name")
    ]
    assert len(production) == 1 and len(rehearsal) == 1

    assert "skip-existing" not in production[0]["with"]
    assert "repository-url" not in production[0]["with"]
    assert rehearsal[0]["with"]["skip-existing"] is True
    assert "test.pypi.org" in rehearsal[0]["with"]["repository-url"]


def test_release_build_toolchain_is_pinned():
    """`test-v*` and `v*` are separate builds; both must use the same tools.

    Checked structurally -- every setup-uv in the release workflow names an
    exact version -- rather than by grepping for the string "latest".
    """
    jobs = _release_workflow()["jobs"]
    setups = [
        step
        for job in jobs.values()
        for step in job["steps"]
        if "setup-uv" in step.get("uses", "")
    ]
    assert setups, "the release workflow should install uv explicitly"
    for step in setups:
        version = (step.get("with") or {}).get("version", "")
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), (
            f"setup-uv version must be exact, got {version!r}"
        )

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    requires = pyproject["build-system"]["requires"]
    assert requires and all("==" in item for item in requires), (
        f"the build backend must be pinned exactly, got {requires}"
    )

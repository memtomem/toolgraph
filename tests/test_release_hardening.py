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
    sse = ServerSpec(
        transport="sse",
        url="https://alice:secret@example.test:8443/sse?token=super-secret#fragment",
    )

    assert endpoint_label(stdio) == "stdio:python"
    assert spec_label(stdio) == "stdio:python"
    assert endpoint_label(http) == "https://example.test:8443"
    # SSE URLs carry the same credentials and query tokens as streamable-http.
    assert endpoint_label(sse) == "https://example.test:8443"
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


# Actions the publish job may run. An allowlist, because the question is not
# "did someone add a checkout" but "what runs in the job that can publish as
# this project" -- anything not named here has to be argued for.
PUBLISH_ALLOWED_ACTIONS = {
    "actions/download-artifact",
    "pypa/gh-action-pypi-publish",
}

# Commands the publish job's one shell step may invoke. Also an allowlist: a
# denylist of scary names is not a boundary, because `eval "$SUMS"` is not on
# anybody's list of scary names until it is.
PUBLISH_ALLOWED_COMMANDS = {
    "VERSION=",  # plain assignments
    "expected=",
    "actual=",
    "printf",
    "cat",
    "test",
    "sha256sum",
}


def _permissions(node: dict) -> dict:
    """Normalize a `permissions:` value.

    It can be a map, or the scalar `read-all` / `write-all`. Treating a scalar
    as "no id-token key" is how `permissions: write-all` -- which grants
    everything, id-token included -- would sail past a naive membership test.
    """
    value = node.get("permissions")
    if value is None:
        return {}
    if isinstance(value, str):
        return {"__scalar__": value}
    return value


def test_publishing_authority_is_isolated_from_build_code():
    """Nothing but the uploader may run in the job that can publish.

    This is the property that makes Trusted Publishing safe to enable: a
    compromised build script or transitive build dependency must not execute
    inside a job that can mint an OIDC token and upload as this project.

    Note what is *not* claimed. `actions/download-artifact` and the PyPI
    publisher are themselves code, pinned by commit; and ordering steps inside
    the build job is not a boundary against a build that is already
    compromised. The claim is that no checked-out project code and no installed
    dependency runs in the privileged job.
    """
    workflow = _release_workflow()
    jobs = workflow["jobs"]

    # Workflow-level permissions are inherited, and the scalar forms grant in
    # bulk -- so both the key and the scalar shorthand have to be excluded.
    top = _permissions(workflow)
    assert "id-token" not in top
    assert top.get("__scalar__") != "write-all"

    privileged = [
        name for name, job in jobs.items()
        if _permissions(job).get("id-token") == "write"
    ]
    assert privileged == ["publish"]

    build = jobs["build"]
    build_perms = _permissions(build)
    # Declared, not inherited: a job with no permissions block takes the
    # workflow's, so silence here is not the same as "unprivileged".
    assert build_perms and "id-token" not in build_perms
    assert build_perms.get("__scalar__") != "write-all"
    assert any(
        "scripts/verify-artifacts.sh" in step.get("run", "")
        for step in build["steps"]
    )

    publish = jobs["publish"]
    needs = publish.get("needs")
    assert needs == "build" or needs == ["build"]
    # `needs:` alone is not a gate: `if: always()` runs the job even when the
    # dependency failed, which would publish artifacts that failed verification.
    assert "if" not in publish, "the publish job must not carry a job-level if:"
    assert not publish.get("continue-on-error")
    assert (publish.get("environment") or {}).get("name") == "pypi"

    run_steps = []
    publisher_indexes = []
    for index, step in enumerate(publish["steps"]):
        assert not step.get("continue-on-error")
        assert "if" not in step or "gh-action-pypi-publish" in step.get("uses", ""), (
            "only the two publisher steps may be conditional"
        )
        if "uses" in step:
            action, _, ref = step["uses"].partition("@")
            assert action in PUBLISH_ALLOWED_ACTIONS, (
                f"{action} would run in the job that can publish"
            )
            assert re.fullmatch(r"[0-9a-f]{40}", ref), (
                f"{action} must be pinned to a commit sha, got {ref!r}"
            )
            if "gh-action-pypi-publish" in action:
                publisher_indexes.append(index)
        if "run" in step:
            run_steps.append((index, step))

    assert len(run_steps) == 1, "the publish job should run one shell step"
    digest_index, digest_step = run_steps[0]
    assert digest_index < min(publisher_indexes), (
        "the digest check must run before anything is published"
    )

    # `${{ }}` inside run: is textual substitution, so a value a compromised
    # build controls would become shell code executing right here.
    assert "${{" not in digest_step["run"]
    assert set(digest_step.get("env", {})) == {"SUMS"}

    for line in digest_step["run"].splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Structure, not commands.
        if line in {"fi", "done", "else"} or line.startswith(("if ", "while ", "for ")):
            continue
        assert any(line.startswith(allowed) for allowed in PUBLISH_ALLOWED_COMMANDS), (
            f"unexpected command in the privileged job: {line!r}"
        )


def test_production_publish_does_not_skip_existing_versions():
    """A production version that already exists is a failure, not a no-op.

    `skip-existing` is right for the TestPyPI rehearsal, which gets re-run. On
    PyPI it would turn a duplicate or partial publication into a green check.

    Keyed on each step's `if:` condition, matched exactly rather than by
    substring, so a condition that merely mentions `test-` while evaluating the
    other way cannot slip past.
    """
    steps = _release_workflow()["jobs"]["publish"]["steps"]
    publishers = [
        step for step in steps
        if step.get("uses", "").startswith("pypa/gh-action-pypi-publish@")
    ]
    assert len(publishers) == 2

    conditions = {step["if"].strip(): step for step in publishers}
    production = conditions["${{ !startsWith(github.ref_name, 'test-') }}"]
    rehearsal = conditions["${{ startsWith(github.ref_name, 'test-') }}"]

    assert "skip-existing" not in production["with"]
    assert "repository-url" not in production["with"]
    assert rehearsal["with"]["skip-existing"] is True
    assert rehearsal["with"]["repository-url"] == "https://test.pypi.org/legacy/"


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

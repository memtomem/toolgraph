"""ADR-0003 CLI contract (issue #12).

Query commands reserve stdout for their JSON — the unsafe-tools
AGENT_NOT_FOUND diagnostic moves to stderr. Exit codes stay advisory
(0) by default; ``check-access --fail-on-deny`` and ``unsafe-tools
--fail-on-violation`` opt a CI pipeline into gating, with exit 2 on
not-found/ambiguous verdicts so a typo can't pass as a clean run.
"""

from __future__ import annotations

import json
import re
import sys

import pytest
import yaml
from typer.testing import CliRunner

from tests._helpers import CLICK_SPLITS_STREAMS, CSV, seed_basic
from toolgraph import __version__, cli
from toolgraph.cli import app
from toolgraph.graph import driver, loader
from toolgraph.manifest.ingest import governance_counts, ingest_governance
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


def _seed(exceptions: list[ExpectedException] | None = None) -> None:
    """planner: read_file DENY-governed, free_tool ungoverned; loner: no grants."""
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file"), ToolRecord(name="free_tool")],
            resources=[ResourceRecord(uri=CSV)],
        )
    )
    report = ingest_governance(
        Governance(
            agents=["planner", "loner"],
            policies=[Policy(id="pii-deny", effect="DENY")],
            grants=[
                AccessGrant(agent="planner", tool="sample::read_file"),
                AccessGrant(agent="planner", tool="sample::free_tool"),
            ],
            data_access=[DataAccess(tool="sample::read_file", resource=CSV, mode="READS")],
            governed_by={CSV: ["pii-deny"]},
            expected_exceptions=exceptions or [],
        )
    )
    assert report.warnings == []


@pytest.fixture
def violating(backend_graph):
    """planner -> read_file is a DENY violation (no exception declared)."""
    _seed()


@pytest.fixture
def authorized(backend_graph):
    """The only DENY evidence is covered by an operator exception."""
    _seed(
        exceptions=[
            ExpectedException(
                agent="planner",
                tool="sample::read_file",
                policy="pii-deny",
                resource=CSV,
                reason="by design",
            )
        ]
    )


# --- check-access --fail-on-deny ----------------------------------------


def test_fail_on_deny_exits_1_on_real_violation(violating):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["check-access", "planner", "read_file", "--fail-on-deny"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)  # JSON printed before the gate fires
    assert payload["verdict"] == "DENY"
    assert payload["all_authorized"] is False


def test_fail_on_deny_exits_0_on_allow(violating):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["check-access", "planner", "free_tool", "--fail-on-deny"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["verdict"] == "ALLOW"


def test_fail_on_deny_exits_0_on_not_granted(violating):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["check-access", "loner", "read_file", "--fail-on-deny"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["verdict"] == "NOT_GRANTED"


def test_fail_on_deny_exits_0_when_deny_is_all_authorized(authorized):
    """An operator-declared exception is by-design — it must not trip the gate."""
    runner = CliRunner()
    result = runner.invoke(cli.app, ["check-access", "planner", "read_file", "--fail-on-deny"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["verdict"] == "DENY"
    assert payload["all_authorized"] is True


def test_fail_on_deny_exits_2_on_unknown_tool(violating):
    """A typo'd tool must not pass a CI gate as a clean run."""
    runner = CliRunner()
    result = runner.invoke(cli.app, ["check-access", "planner", "no_such_tool", "--fail-on-deny"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["verdict"] == "TOOL_NOT_FOUND"


def test_fail_on_deny_exits_2_on_unknown_agent(violating):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["check-access", "planer", "read_file", "--fail-on-deny"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["verdict"] == "AGENT_NOT_FOUND"


def test_check_access_without_flag_still_exits_0_on_deny(violating):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["check-access", "planner", "read_file"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["verdict"] == "DENY"


# --- unsafe-tools --fail-on-violation -----------------------------------


def test_fail_on_violation_exits_1_with_violation(violating):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["unsafe-tools", "planner", "--fail-on-violation"])
    assert result.exit_code == 1
    rows = json.loads(result.stdout)  # JSON printed before the gate fires
    assert any(r["classification"] == "violation" for r in rows)


def test_fail_on_violation_with_all_still_gates(violating):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["unsafe-tools", "planner", "--all", "--fail-on-violation"])
    assert result.exit_code == 1


def test_fail_on_violation_exits_0_when_clean(authorized):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["unsafe-tools", "planner", "--fail-on-violation"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == []

    # --all surfaces the authorized_but_governed row, but it's not a violation.
    full = runner.invoke(cli.app, ["unsafe-tools", "planner", "--all", "--fail-on-violation"])
    assert full.exit_code == 0
    assert all(r["classification"] == "authorized_but_governed" for r in json.loads(full.stdout))


def test_unsafe_tools_without_flag_still_exits_0_on_violation(violating):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["unsafe-tools", "planner"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)


# --- audit-report --------------------------------------------------------


def test_audit_report_stdout_is_json_by_default(violating):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["audit-report"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "fail"
    assert payload["summary"]["counts"]["unsafe_violations"] == 1


def test_audit_report_markdown_format(violating):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["audit-report", "--format", "markdown"])
    assert result.exit_code == 0
    assert result.stdout.startswith("# toolgraph audit report")
    assert "| `unsafe_violations` | 1 | yes |" in result.stdout


def test_audit_report_fail_on_blocking(violating):
    runner = CliRunner()
    result = runner.invoke(cli.app, ["audit-report", "--fail-on-blocking"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["status"] == "fail"


# --- stdout purity (ADR-0003 §1) ----------------------------------------


def test_unsafe_tools_unknown_agent_keeps_stdout_json_pure(violating):
    """`toolgraph unsafe-tools bot | jq` must not choke on the diagnostic:
    stdout is empty (no JSON was produced), AGENT_NOT_FOUND goes to stderr."""
    runner = CliRunner()
    result = runner.invoke(cli.app, ["unsafe-tools", "planer"])
    assert result.exit_code == 1
    if CLICK_SPLITS_STREAMS:
        assert result.stdout.strip() == ""
        assert "AGENT_NOT_FOUND" in result.stderr
    else:
        # Old click folds stderr into output — purity is unassertable; the
        # diagnostic itself must still appear.
        assert "AGENT_NOT_FOUND" in result.output


def test_unsafe_tools_known_agent_stdout_is_json_only(violating):
    if not CLICK_SPLITS_STREAMS:
        pytest.skip("click < 8.2 mixes stderr into stdout")
    runner = CliRunner()
    result = runner.invoke(cli.app, ["unsafe-tools", "planner"])
    assert result.exit_code == 0
    json.loads(result.stdout)  # the whole stream parses
    assert result.stderr == ""


def test_check_never_prints_neo4j_credentials(monkeypatch):
    from toolgraph import config
    from toolgraph.graph import driver

    monkeypatch.setitem(
        vars(config),
        "settings",
        config.Settings(
            backend="neo4j",
            neo4j_uri="neo4j+s://neo4j:hunter2@db.example:7687?token=abc",
        ),
    )
    monkeypatch.setattr(driver, "verify_connectivity", lambda **_: None)

    result = CliRunner().invoke(cli.app, ["check"])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "OK: backend=neo4j uri=neo4j+s://db.example:7687"


def test_init_reports_unusable_state_dir_without_traceback(tmp_path):
    pytest.importorskip("ladybug")
    blocker = tmp_path / "a-file"
    blocker.write_text("")

    result = CliRunner().invoke(cli.app, ["init", "--state-dir", str(blocker / "state")])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert result.stderr.startswith(f"ERROR: cannot initialize {blocker / 'state'}: ")
    assert not (blocker / "state").exists()


def test_init_warns_when_a_partial_database_cannot_be_removed(tmp_path, monkeypatch):
    from pathlib import Path

    from toolgraph.graph import schema

    state_dir = tmp_path / "state"
    db_path = state_dir / "toolgraph.lbug"

    def half_created():
        state_dir.mkdir(parents=True, exist_ok=True)
        db_path.write_bytes(b"partial")
        raise RuntimeError("schema failed")

    real_unlink = Path.unlink

    def refuse(self, *args, **kwargs):
        if self == db_path:
            raise PermissionError(13, "Permission denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(schema, "init_schema", half_created)
    monkeypatch.setattr(Path, "unlink", refuse)

    result = CliRunner().invoke(cli.app, ["init", "--state-dir", str(state_dir)])

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    assert result.stderr.splitlines() == [
        f"WARNING: could not remove partial database {db_path}: Permission denied"
    ]
    assert db_path.exists()


def test_cli_ingest_manifest_says_rejected_on_warnings(backend_graph, tmp_path):
    """The CLI must NOT say 'Ingested' when ingest rejected the manifest —
    that contradicts the atomicity guarantee directly to the user."""
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file")],
            resources=[ResourceRecord(uri=CSV)],
        )
    )
    manifest_path = tmp_path / "governance.yaml"
    manifest_path.write_text(
        yaml.safe_dump(
            {
                "agents": ["planner"],
                "policies": [{"id": "real", "effect": "DENY"}],
                "grants": [{"agent": "planner", "tool": "sample::read_file"}],
                "governed_by": {CSV: ["typo-policy"]},  # forces rejection
            }
        )
    )
    runner = CliRunner()
    result = runner.invoke(cli.app, ["ingest-manifest", "--governance", str(manifest_path)])
    assert result.exit_code == 1
    assert "REJECTED" in result.output
    assert "graph unchanged" in result.output
    assert "Ingested governance from" not in result.output
    # And the graph was NOT mutated — counts stay zero
    assert governance_counts()["governed_by"] == 0


def test_cli_unsafe_tools_default_filters_to_violations(backend_graph):
    """`toolgraph unsafe-tools agent` defaults to violation-only output;
    --all surfaces authorized_but_governed rows alongside."""
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file"), ToolRecord(name="write_file")],
            resources=[ResourceRecord(uri=CSV)],
        )
    )
    ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="pii-deny", effect="DENY")],
            grants=[
                AccessGrant(agent="planner", tool="sample::read_file"),
                AccessGrant(agent="planner", tool="sample::write_file"),
            ],
            data_access=[
                DataAccess(tool="sample::read_file", resource=CSV, mode="READS"),
                DataAccess(tool="sample::write_file", resource=CSV, mode="WRITES"),
            ],
            governed_by={CSV: ["pii-deny"]},
            expected_exceptions=[
                ExpectedException(
                    agent="planner",
                    tool="sample::read_file",
                    policy="pii-deny",
                    resource=CSV,
                    reason="by design",
                )
            ],
        )
    )
    runner = CliRunner()

    def classified(*args):
        result = runner.invoke(cli.app, ["unsafe-tools", "planner", *args])
        assert result.exit_code == 0, result.output
        return {(row["tool_key"], row["classification"]) for row in json.loads(result.stdout)}

    assert classified() == {("sample::write_file", "violation")}
    assert classified("--all") == {
        ("sample::write_file", "violation"),
        ("sample::read_file", "authorized_but_governed"),
    }


def test_cli_unsafe_tools_unknown_agent_emits_agent_not_found(backend_graph):
    """A typo'd agent at the CLI must NOT silently return [] — that's the
    exact false-clean the analyzer is supposed to prevent."""
    seed_basic(backend_graph)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["unsafe-tools", "planer"])
    assert result.exit_code == 1
    assert "AGENT_NOT_FOUND" in result.output


def test_cli_unbacked_edges_default_excludes_can_call(backend_graph):
    """`toolgraph unbacked-edges` defaults to READS/WRITES/GOVERNED_BY;
    --include-grants widens to CAN_CALL too."""
    seed_basic(backend_graph)
    runner = CliRunner()

    def edge_types(*args):
        result = runner.invoke(cli.app, ["unbacked-edges", *args])
        assert result.exit_code == 0, result.output
        return {row["edge_type"] for row in json.loads(result.stdout)}

    assert edge_types() == {"READS", "GOVERNED_BY"}
    assert edge_types("--include-grants") == {"READS", "GOVERNED_BY", "CAN_CALL"}


def test_invalid_governance_cli_is_concise_and_does_not_mutate_graph(backend_graph, tmp_path):
    path = tmp_path / "governance.yaml"
    path.write_text("agents: [existing]\nagents: [replacement]\n")
    with driver.session() as current:
        before = current.run("MATCH (n) RETURN count(n) AS count").single()["count"]

    result = CliRunner().invoke(app, ["ingest-manifest", "--governance", str(path)])

    assert result.exit_code == 1
    assert "Invalid governance config" in result.output
    assert "duplicate key 'agents'" in result.output
    assert "Traceback" not in result.output
    with driver.session() as current:
        after = current.run("MATCH (n) RETURN count(n) AS count").single()["count"]
    assert after == before


def test_example_init_creates_self_contained_assets_and_refuses_collision(tmp_path):
    destination = tmp_path / "quickstart"
    runner = CliRunner()

    created = runner.invoke(app, ["example", "init", str(destination)])
    assert created.exit_code == 0, created.output
    assert sorted(path.name for path in destination.iterdir()) == [
        "README.md",
        "governance.yaml",
        "policy_gateway_server.py",
        "servers.yaml",
    ]
    # The generated directory is the only guidance a PyPI user has on disk:
    # `docs/` is excluded from the sdist, so the beginner guide is a URL away.
    readme = (destination / "README.md").read_text()
    assert "toolgraph init" in readme
    assert "docs/beginner-guide.md" in readme
    # The guide claims `unbacked-edges` comes back empty for this fixture; that
    # only holds while every authored edge carries a non-blank evidence
    # pointer. Counting the string would pass on an empty value, so parse it.
    governance = yaml.safe_load((destination / "governance.yaml").read_text())
    authored = [
        *governance["grants"],
        *governance["data_access"],
        *(binding for bindings in governance["governed_by"].values() for binding in bindings),
    ]
    assert len(authored) == 5
    for edge in authored:
        evidence = edge["provenance"]["evidence"]
        assert isinstance(evidence, str) and evidence.strip(), edge

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


def test_json_options_are_rejected_before_graph_or_file_access(monkeypatch):
    monkeypatch.setattr(
        cli, "run_propose", lambda *a, **k: pytest.fail("must reject before graph access")
    )
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["policy", "propose", "missing.yaml", "missing.jsonl", "--json", "--output", "unused.md"],
    )
    assert result.exit_code == 2
    assert "cannot be combined" in re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    result = runner.invoke(cli.app, ["ingest-manifest", "--json"])
    assert result.exit_code == 2
    assert "requires --dry-run" in re.sub(r"\x1b\[[0-9;]*m", "", result.output)


@pytest.mark.parametrize("dry_run", [False, True])
def test_manifest_text_warnings_are_redacted(monkeypatch, tmp_path, dry_run):
    from toolgraph.manifest.ingest import GovernanceDiffReport

    report = GovernanceDiffReport(
        applied=False, warnings=["resource https://user:password@host/x?token=secret"]
    )
    monkeypatch.setattr(
        cli, "dry_run_ingest" if dry_run else "ingest_governance", lambda *a, **k: report
    )
    monkeypatch.setattr(cli.schema, "init_schema", lambda: None)
    path = tmp_path / "governance.yaml"
    path.write_text("agents: [bot]\n")
    result = CliRunner().invoke(
        cli.app, ["ingest-manifest", "--governance", str(path)] + (["--dry-run"] if dry_run else [])
    )
    assert result.exit_code == 1
    assert "password" not in result.output and "secret" not in result.output
    assert "https://host" in result.output


@pytest.mark.parametrize(
    ("document", "detail"),
    [
        (
            "agents: [existing]\n'?%74oken=SECRET': 1\n'?%74oken=SECRET': 2\n",
            "details withheld (they contain credential-shaped data)",
        ),
        (
            "agents: [existing]\n'?%74oken=SECRET': 1\n",
            "1 validation error for Governance: <key>: unknown field [type=extra_forbidden]",
        ),
    ],
    ids=["duplicate-key", "unknown-key"],
)
def test_invalid_governance_error_never_quotes_a_credential_key(tmp_path, document, detail):
    path = tmp_path / "governance.yaml"
    path.write_text(document)

    result = CliRunner().invoke(app, ["ingest-manifest", "--governance", str(path)])

    assert result.exit_code == 1
    assert result.output.strip().endswith(f"governance.yaml: {detail}")
    assert "SECRET" not in result.output

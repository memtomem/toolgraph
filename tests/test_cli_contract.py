"""ADR-0003 CLI contract (issue #12).

Query commands reserve stdout for their JSON — the unsafe-tools
AGENT_NOT_FOUND diagnostic moves to stderr. Exit codes stay advisory
(0) by default; ``check-access --fail-on-deny`` and ``unsafe-tools
--fail-on-violation`` opt a CI pipeline into gating, with exit 2 on
not-found/ambiguous verdicts so a typo can't pass as a clean run.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys

import pytest
import yaml
from typer.testing import CliRunner

from tests._helpers import CSV, flat_output, seed_basic, usage_error_text
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
from toolgraph.redaction import WITHHELD, display_path


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
    assert result.stdout.strip() == ""
    assert "AGENT_NOT_FOUND" in result.stderr


def test_unsafe_tools_known_agent_stdout_is_json_only(violating):
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
    assert result.output.strip().endswith(
        "governance.yaml: invalid YAML at line 2, column 1: duplicate key"
    )
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
    assert flat_output(result.stderr) == usage_error_text(
        "ingest-manifest [OPTIONS]", "Invalid value for '--json': requires --dry-run"
    )


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
            "invalid YAML at line 3, column 1: duplicate key",
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


# --- flag values and the backend are checked before any work -------------

UNKNOWN_PROFILE = (
    "Invalid value for '--profile': unknown profile 'bogus' "
    "— expected one of ['explore', 'review', 'strict']"
)


@pytest.mark.parametrize(
    ("args", "usage"),
    [
        (["eligible-tools", "a", "x"], "eligible-tools [OPTIONS] {agent} {candidates}..."),
        (["selection-explain", "a", "x"], "selection-explain [OPTIONS] {agent} {tool}"),
        (["preflight", "a", "x", "--run-id", "r"], "preflight [OPTIONS] {agent} {candidates}..."),
        (["control-preflight", "plan.json"], "control-preflight [OPTIONS] {plan}"),
        (
            ["policy", "impact", "g.yaml", "x", "--agent", "a"],
            "policy impact [OPTIONS] {governance} {candidates}...",
        ),
        (["policy", "compile", "--agent", "a", "--output", "b.json"], "policy compile [OPTIONS]"),
        (
            ["policy", "review-plan", "report.json", "--agent", "a", "--output", "plan.json"],
            "policy review-plan [OPTIONS] {report}",
        ),
    ],
    ids=[
        "eligible-tools",
        "selection-explain",
        "preflight",
        "control-preflight",
        "policy-impact",
        "policy-compile",
        "policy-review-plan",
    ],
)
def test_an_unknown_profile_names_its_flag(args, usage):
    result = CliRunner().invoke(cli.app, [*args, "--profile", "bogus"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert flat_output(result.stderr) == usage_error_text(usage, UNKNOWN_PROFILE)


@pytest.mark.parametrize(
    ("args", "usage", "message"),
    [
        (
            ["--config", "missing.json", "check"],
            "[OPTIONS] COMMAND [ARGS]...",
            "Invalid value for '--config': Toolgraph runtime config does not exist: {missing}",
        ),
        (
            ["example", "init", "exists"],
            "example init [OPTIONS] [destination]",
            "Invalid value for 'DESTINATION': destination already exists: {exists}",
        ),
        (
            ["init", "--backend", "bogus"],
            "init [OPTIONS]",
            "Invalid value for '--backend': expected ladybug or neo4j",
        ),
        (
            ["eligible-tools", "a", *["x"] * 4097],
            "eligible-tools [OPTIONS] {{agent}} {{candidates}}...",
            "Invalid value for 'CANDIDATES': 4097 candidates exceed the 4096 limit",
        ),
        (
            ["review-candidates", "annotate", "r.json", "c", "--status", "accepted"]
            + ["--reviewer", "   "],
            "review-candidates annotate [OPTIONS] {{report}} {{candidate_id}}",
            "Invalid value for '--reviewer': "
            "reviewer must be a non-empty single line of at most 256 characters",
        ),
        (
            ["review-candidates", "annotate", "r.json", "c", "--status", "accepted"]
            + ["--reviewer", "operator", "--note", "a\nb"],
            "review-candidates annotate [OPTIONS] {{report}} {{candidate_id}}",
            "Invalid value for '--note': "
            "note must be a non-empty single line of at most 500 characters",
        ),
    ],
    ids=["config", "destination", "backend", "candidates", "reviewer", "note"],
)
def test_a_parameter_named_in_a_usage_error_is_quoted(
    tmp_path, monkeypatch, fresh_settings, args, usage, message
):
    """Toolgraph's own BadParameter hints read like the ones Click derives."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "exists").mkdir()
    paths = {
        "missing": display_path((tmp_path / "missing.json").resolve()),
        "exists": display_path((tmp_path / "exists").resolve()),
    }

    result = CliRunner().invoke(cli.app, args)

    assert result.exit_code == 2
    assert result.stdout == ""
    # A long path wraps inside a word in the error box (#250): compare
    # without whitespace.
    expected = usage_error_text(usage.format(**paths), message.format(**paths))
    assert "".join(flat_output(result.stderr).split()) == "".join(expected.split())


@pytest.mark.parametrize("value", ["0", "-1", "nan", "-inf", "1e-400"])
def test_a_crawl_timeout_not_above_zero_is_a_usage_error(monkeypatch, value):
    monkeypatch.setattr(cli, "crawl_all", lambda *a, **k: pytest.fail("must not crawl"))

    result = CliRunner().invoke(cli.app, ["crawl", "--timeout", value])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert flat_output(result.stderr) == usage_error_text(
        "crawl [OPTIONS]", "Invalid value for '--timeout': must be a positive number of seconds"
    )


def test_a_crawl_timeout_of_inf_crawls_without_one(backend_graph, tmp_path, monkeypatch):
    servers = tmp_path / "servers.yaml"
    servers.write_text("servers: []\n", encoding="utf-8")
    seen = []

    def crawl_all(specs, *, timeout, budget):
        seen.append(timeout)
        return [], []

    monkeypatch.setattr(cli, "crawl_all", crawl_all)
    result = CliRunner().invoke(
        cli.app, ["crawl", "--servers", str(servers), "--timeout", "inf", "--no-prune"]
    )

    assert result.exit_code == 0, result.output
    assert seen == [math.inf]


@pytest.mark.parametrize("force_color", [False, True])
@pytest.mark.parametrize(
    ("args", "flag"),
    [(["--timeout", "-1"], "--timeout"), (["--max-catalog-records", "0"], "--max-catalog-records")],
)
def test_a_range_error_is_withheld_whole_beside_a_credential(monkeypatch, args, flag, force_color):
    """The value is a plain number, but another token is credential-shaped, so
    the message is withheld whole, as for every other usage error."""
    if force_color:
        monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setattr(cli, "crawl_all", lambda *a, **k: pytest.fail("must not crawl"))

    result = CliRunner().invoke(cli.app, ["crawl", "--servers", "password=QXZQ", *args])

    assert result.exit_code == 2
    assert flat_output(result.stderr) == usage_error_text(
        "crawl [OPTIONS]", f"Invalid value for '{flag}': {WITHHELD}"
    )
    assert "QXZQ" not in result.output


@pytest.fixture
def no_backend(tmp_path, monkeypatch, fresh_settings):
    """A working directory where nothing names a backend: no runtime config,
    no `.env`, no TOOLGRAPH_* variables, no cached settings."""
    for name in ("TOOLGRAPH_BACKEND", "TOOLGRAPH_DB_PATH", "TOOLGRAPH_CONFIG", "TOOLGRAPH_SERVERS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    driver.close_driver()
    yield tmp_path
    driver.close_driver()


NO_BACKEND = (
    "ERROR: no backend configured: run `toolgraph init`, or set TOOLGRAPH_BACKEND "
    "to ladybug or neo4j\n"
)
UNKNOWN_BACKEND = "ERROR: unknown TOOLGRAPH_BACKEND 'bogus'; expected 'neo4j' or 'ladybug'\n"
NO_NEO4J_EXTRA = (
    "ERROR: backend=neo4j requires the 'neo4j' extra: "
    "install with `uv sync --extra neo4j` or `pip install toolgraph[neo4j]`\n"
)


@pytest.mark.parametrize(
    ("backend", "expected"),
    [(None, NO_BACKEND), ("bogus", UNKNOWN_BACKEND), ("neo4j", NO_NEO4J_EXTRA)],
    ids=["none", "unknown", "neo4j-without-extra"],
)
def test_crawl_refuses_an_unusable_backend_before_crawling(
    no_backend, monkeypatch, backend, expected
):
    if backend is not None:
        monkeypatch.setenv("TOOLGRAPH_BACKEND", backend)
    # A None entry makes `import neo4j` raise ImportError.
    monkeypatch.setitem(sys.modules, "neo4j", None)
    monkeypatch.setattr(cli, "crawl_all", lambda *a, **k: pytest.fail("must not crawl"))
    servers = no_backend / "fleet.yaml"
    servers.write_text("servers: []\n", encoding="utf-8")

    result = CliRunner().invoke(cli.app, ["crawl", "--servers", str(servers)])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == expected


@pytest.mark.parametrize(
    ("backend", "servers_flag", "first"),
    [
        # A servers file named on the command line is read first ...
        (None, True, "Invalid servers config at "),
        # ... while the default one is found through the settings, so a
        # backend that cannot be resolved is refused before it is read ...
        (None, False, NO_BACKEND),
        # ... but an unknown name resolves, and is refused after the file.
        ("bogus", False, "Invalid servers config at "),
    ],
    ids=["servers-flag", "default-servers", "default-servers-unknown-name"],
)
def test_crawl_error_precedence_is_unchanged(no_backend, monkeypatch, backend, servers_flag, first):
    if backend is not None:
        monkeypatch.setenv("TOOLGRAPH_BACKEND", backend)
    monkeypatch.setattr(cli, "crawl_all", lambda *a, **k: pytest.fail("must not crawl"))
    args = ["crawl", "--servers", str(no_backend / "missing.yaml")] if servers_flag else ["crawl"]

    result = CliRunner().invoke(cli.app, args)

    assert result.exit_code == 1
    assert result.stderr.startswith(first)


def test_crawl_launches_no_server_when_the_backend_is_unusable(tmp_path):
    """Run as a real process: a server writes a marker when it is launched."""
    marker = tmp_path / "launched"
    servers = tmp_path / "servers.yaml"
    probe = {
        "name": "probe",
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-c", f"open({str(marker)!r}, 'w').close()"],
    }
    servers.write_text(yaml.safe_dump({"servers": [probe]}), encoding="utf-8")
    base = {key: value for key, value in os.environ.items() if not key.startswith("TOOLGRAPH_")}

    def crawl(env):
        return subprocess.run(
            [sys.executable, "-m", "toolgraph.cli", "crawl", "--servers", str(servers)]
            + ["--timeout", "20", "--no-prune"],
            capture_output=True,
            text=True,
            check=False,
            cwd=tmp_path,
            env=env,
            timeout=120,
        )

    refused = crawl({**base, "TOOLGRAPH_BACKEND": "bogus"})
    assert refused.returncode == 1
    assert refused.stdout == ""
    assert refused.stderr == UNKNOWN_BACKEND
    assert not marker.exists()

    # Control: the same file does launch the server once the backend is usable.
    launched = crawl(
        {**base, "TOOLGRAPH_BACKEND": "ladybug", "TOOLGRAPH_DB_PATH": str(tmp_path / "g.lbug")}
    )
    assert launched.stdout.startswith("Crawling 1 server(s)"), launched.stderr
    assert marker.exists()

"""ADR-0003 CLI contract (issue #12).

Query commands reserve stdout for their JSON — the unsafe-tools
AGENT_NOT_FOUND diagnostic moves to stderr. Exit codes stay advisory
(0) by default; ``check-access --fail-on-deny`` and ``unsafe-tools
--fail-on-violation`` opt a CI pipeline into gating, with exit 2 on
not-found/ambiguous verdicts so a typo can't pass as a clean run.
"""

from __future__ import annotations

import json
from importlib.metadata import version

import pytest
from typer.testing import CliRunner

from toolgraph import cli
from toolgraph.graph import loader
from toolgraph.manifest.ingest import ingest_governance
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

CSV = "file:///data/customers.csv"

# click >= 8.2 always splits streams (result.stdout / result.stderr);
# older click mixes stderr into stdout by default, so purity can't be
# asserted there.
CLICK_SPLITS_STREAMS = tuple(
    int(p) for p in version("click").split(".")[:2]
) >= (8, 2)


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
def violating(graph):
    """planner -> read_file is a DENY violation (no exception declared)."""
    _seed()


@pytest.fixture
def authorized(graph):
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
    result = runner.invoke(
        cli.app, ["check-access", "planner", "read_file", "--fail-on-deny"]
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)  # JSON printed before the gate fires
    assert payload["verdict"] == "DENY"
    assert payload["all_authorized"] is False


def test_fail_on_deny_exits_0_on_allow(violating):
    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["check-access", "planner", "free_tool", "--fail-on-deny"]
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["verdict"] == "ALLOW"


def test_fail_on_deny_exits_0_on_not_granted(violating):
    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["check-access", "loner", "read_file", "--fail-on-deny"]
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["verdict"] == "NOT_GRANTED"


def test_fail_on_deny_exits_0_when_deny_is_all_authorized(authorized):
    """An operator-declared exception is by-design — it must not trip the gate."""
    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["check-access", "planner", "read_file", "--fail-on-deny"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["verdict"] == "DENY"
    assert payload["all_authorized"] is True


def test_fail_on_deny_exits_2_on_unknown_tool(violating):
    """A typo'd tool must not pass a CI gate as a clean run."""
    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["check-access", "planner", "no_such_tool", "--fail-on-deny"]
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["verdict"] == "TOOL_NOT_FOUND"


def test_fail_on_deny_exits_2_on_unknown_agent(violating):
    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["check-access", "planer", "read_file", "--fail-on-deny"]
    )
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
    result = runner.invoke(
        cli.app, ["unsafe-tools", "planner", "--fail-on-violation"]
    )
    assert result.exit_code == 1
    rows = json.loads(result.stdout)  # JSON printed before the gate fires
    assert any(r["classification"] == "violation" for r in rows)


def test_fail_on_violation_with_all_still_gates(violating):
    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["unsafe-tools", "planner", "--all", "--fail-on-violation"]
    )
    assert result.exit_code == 1


def test_fail_on_violation_exits_0_when_clean(authorized):
    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["unsafe-tools", "planner", "--fail-on-violation"]
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout) == []

    # --all surfaces the authorized_but_governed row, but it's not a violation.
    full = runner.invoke(
        cli.app, ["unsafe-tools", "planner", "--all", "--fail-on-violation"]
    )
    assert full.exit_code == 0
    assert all(
        r["classification"] == "authorized_but_governed"
        for r in json.loads(full.stdout)
    )


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

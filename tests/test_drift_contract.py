"""ADR-0002 drift contract (issue #9).

A tool the loader kept (authored governance, no live EXPOSES) used to be
silently re-grantable forever: ingest resolution checked node existence
only, so re-ingesting the same manifest re-targeted the stale node with
zero signal. Now ingest emits a non-fatal DRIFT notice per such ref, and
``strict_drift`` promotes those notices to rejecting warnings for CI.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from toolgraph import cli
from toolgraph.graph import loader
from toolgraph.manifest.ingest import governance_counts, ingest_governance
from toolgraph.models import (
    AccessGrant,
    CrawlResult,
    Governance,
    Policy,
    ToolRecord,
)


def _crawl(tools: list[str]):
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name=t) for t in tools],
        )
    )


def _gov() -> Governance:
    return Governance(
        agents=["planner"],
        policies=[Policy(id="legacy-deny", effect="DENY")],
        grants=[AccessGrant(agent="planner", tool="sample::old_tool")],
        governed_by={"sample::old_tool": ["legacy-deny"]},
    )


@pytest.fixture
def drifted(graph):
    """old_tool granted+governed, then the server stops exposing it."""
    _crawl(["old_tool"])
    report = ingest_governance(_gov())
    assert report.warnings == [] and report.notices == []
    _crawl(["new_tool"])  # re-crawl: old_tool kept (governed), EXPOSES dropped


def test_reingest_emits_drift_notice_but_still_applies(drifted):
    report = ingest_governance(_gov())
    assert report.warnings == []
    assert report.applied is True
    assert len(report.notices) == 1
    assert "DRIFT" in report.notices[0] and "sample::old_tool" in report.notices[0]
    # The apply still happened — default mode must not block a transition.
    assert governance_counts()["can_call"] == 1


def test_strict_drift_rejects_whole_manifest(drifted):
    report = ingest_governance(_gov(), strict_drift=True)
    assert report.applied is False
    assert any("DRIFT" in w for w in report.warnings)
    # Atomicity: rejection left the previous (first ingest) edges in place.
    assert governance_counts()["can_call"] == 1


def test_clean_manifest_has_no_notices(graph):
    _crawl(["read_file"])
    report = ingest_governance(
        Governance(
            agents=["planner"],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
        )
    )
    assert report.warnings == [] and report.notices == []


def test_cli_strict_drift_flag_exits_nonzero(drifted, tmp_path):
    manifest = tmp_path / "governance.yaml"
    manifest.write_text(
        """
agents: [planner]
policies:
  - {id: legacy-deny, effect: DENY}
grants:
  - {agent: planner, tool: "sample::old_tool"}
governed_by:
  "sample::old_tool": [legacy-deny]
"""
    )
    runner = CliRunner()
    default = runner.invoke(cli.app, ["ingest-manifest", "--governance", str(manifest)])
    assert default.exit_code == 0
    assert "DRIFT" in default.output  # notice surfaced, ingest applied

    strict = runner.invoke(
        cli.app, ["ingest-manifest", "--governance", str(manifest), "--strict-drift"]
    )
    assert strict.exit_code == 1
    assert "REJECTED" in strict.output

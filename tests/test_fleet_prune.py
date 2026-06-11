"""ADR-0007 fleet-level crawl reconciliation (issue #25).

Per-server reconciliation only fires when that server is crawled, so a
server removed (or renamed) in servers.yaml used to leave its whole crawled
subgraph live: stale EXPOSES defeated every drift check, stale hints kept
feeding the annotation audits, and bare-name twins made every bare ref
AMBIGUOUS_TOOL. ``retire_unlisted_servers`` prunes at fleet level with the
same keep-rule as per-server reconciliation; ``fleet_keep_names`` decides
who survives, and refuses to guess identities for unnamed crawl failures.
"""

from __future__ import annotations

import yaml
from typer.testing import CliRunner

from toolgraph import cli
from toolgraph.crawler.crawl import (
    duplicate_identities,
    duplicate_name_overrides,
    fleet_keep_names,
)
from toolgraph.graph import driver, loader, queries
from toolgraph.manifest.ingest import ingest_governance
from toolgraph.models import (
    AccessGrant,
    CrawlResult,
    Governance,
    Policy,
    ResourceRecord,
    ServerSpec,
    ToolRecord,
)


def _load(server: str, tools: list[ToolRecord] = (), resources: list[str] = ()) -> None:
    loader.load_crawl_result(
        CrawlResult(
            server_name=server,
            transport="stdio",
            tools=list(tools),
            resources=[ResourceRecord(uri=u) for u in resources],
        )
    )


def _generation() -> int | None:
    with driver.session() as s:
        rec = s.run(
            "MATCH (m:GraphMeta {id:'singleton'}) RETURN m.generation AS g"
        ).single()
        return rec["g"] if rec else None


def _server_names() -> set[str]:
    with driver.session() as s:
        return {r["n"] for r in s.run("MATCH (s:MCPServer) RETURN s.name AS n")}


# --- keep-set computation (pure, no graph) --------------------------------


def test_keep_is_named_specs_plus_crawled_names():
    specs = [
        ServerSpec(name="pinned", command="cmd-a"),
        ServerSpec(command="cmd-b"),
    ]
    results = [CrawlResult(server_name="self-reported", transport="stdio")]
    keep, blockers = fleet_keep_names(specs, results, [])
    assert keep == {"pinned", "self-reported"}
    assert blockers == []


def test_named_failure_is_protected_not_blocking():
    spec = ServerSpec(name="flaky", command="cmd")
    keep, blockers = fleet_keep_names([spec], [], [(spec, "timeout after 1s")])
    assert keep == {"flaky"}
    assert blockers == []


def test_unnamed_failure_blocks_the_prune():
    spec = ServerSpec(command="npx flaky-server")
    keep, blockers = fleet_keep_names([spec], [], [(spec, "timeout after 1s")])
    assert blockers == ["npx flaky-server"]


def test_label_collision_does_not_protect_unnamed_failure():
    """An unnamed spec whose command equals another spec's name is still an
    unidentifiable failure — blocker detection must ride on the spec, not on
    a display label (Codex review of this branch caught it)."""
    named = ServerSpec(name="npx flaky-server", command="cmd-a")
    unnamed = ServerSpec(command="npx flaky-server")
    results = [CrawlResult(server_name="npx flaky-server", transport="stdio")]
    _, blockers = fleet_keep_names(
        [named, unnamed], results, [(unnamed, "timeout after 1s")]
    )
    assert blockers == ["npx flaky-server"]


def test_empty_fleet_keeps_nothing():
    keep, blockers = fleet_keep_names([], [], [])
    assert keep == set() and blockers == []


def test_duplicate_name_overrides_detected():
    specs = [
        ServerSpec(name="dup", command="cmd-a"),
        ServerSpec(name="dup", command="cmd-b"),
        ServerSpec(name="solo", command="cmd-c"),
    ]
    assert duplicate_name_overrides(specs) == {"dup"}


def test_duplicate_identities_across_results_and_named_failures():
    # An unnamed server self-reporting a failed named spec's name is the
    # same identity collision, one crawl later.
    failed = ServerSpec(name="pinned", command="cmd-a")
    results = [CrawlResult(server_name="pinned", transport="stdio")]
    assert duplicate_identities(results, [(failed, "boom")]) == {"pinned"}
    assert duplicate_identities(results, []) == set()


# --- retirement semantics (real graph) ------------------------------------


def test_removed_server_subgraph_is_retired(graph):
    _load("alpha", [ToolRecord(name="read_file")])
    _load("beta", [ToolRecord(name="write_file")])

    retired, warnings = loader.retire_unlisted_servers({"alpha"})

    assert retired == ["beta"]
    assert warnings == []
    assert _server_names() == {"alpha"}
    counts = loader.graph_counts()
    assert counts["tool"] == 1 and counts["exposes"] == 1


def test_retire_is_idempotent_and_noop_keeps_generation(graph):
    _load("alpha", [ToolRecord(name="read_file")])
    _load("beta", [ToolRecord(name="write_file")])
    loader.retire_unlisted_servers({"alpha"})

    gen = _generation()
    retired, warnings = loader.retire_unlisted_servers({"alpha"})

    assert retired == [] and warnings == []
    # ADR-0004: a no-op reconciliation must not invalidate caches.
    assert _generation() == gen


def test_retire_bumps_generation_when_something_changed(graph):
    _load("alpha", [ToolRecord(name="read_file")])
    _load("beta", [ToolRecord(name="write_file")])
    gen = _generation()
    loader.retire_unlisted_servers({"alpha"})
    assert _generation() == gen + 1


def test_governed_tool_kept_drifted_with_hints_cleared(graph):
    _load(
        "beta",
        [ToolRecord(name="drop_table", destructive_hint=True, read_only_hint=False)],
    )
    report = ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="prod-deny", effect="DENY")],
            grants=[AccessGrant(agent="planner", tool="beta::drop_table")],
            governed_by={"beta::drop_table": ["prod-deny"]},
        )
    )
    assert report.warnings == []

    retired, warnings = loader.retire_unlisted_servers(set())

    assert retired == ["beta"]
    assert len(warnings) == 1 and "beta::drop_table" in warnings[0]
    # Kept, but honestly drifted: no EXPOSES means the drift queries see it.
    assert [t["tool_key"] for t in queries.drifted_tools()] == ["beta::drop_table"]
    # ADR-0006: a retired server makes no annotation claims — the hints are
    # gone, so the annotation audits stop citing them.
    with driver.session() as s:
        rec = s.run(
            "MATCH (t:Tool {key:'beta::drop_table'}) "
            "RETURN t.destructive_hint AS d, t.read_only_hint AS r"
        ).single()
    assert rec["d"] is None and rec["r"] is None
    assert queries.destructive_unsafeguarded() == []


def test_resource_lifecycle_follows_adr_0001(graph):
    _load("alpha", resources=["file:///shared.csv"])
    _load("beta", resources=["file:///shared.csv", "file:///b-only.csv", "file:///gov.csv"])
    report = ingest_governance(
        Governance(
            policies=[Policy(id="pii", effect="DENY")],
            governed_by={"file:///gov.csv": ["pii"]},
        )
    )
    assert report.warnings == []

    loader.retire_unlisted_servers({"alpha"})

    with driver.session() as s:
        uris = {r["u"] for r in s.run("MATCH (r:Resource) RETURN r.uri AS u")}
    # Dead crawl artifact gone; shared (still provided) and governed kept.
    assert uris == {"file:///shared.csv", "file:///gov.csv"}
    assert loader.graph_counts()["provides"] == 1


def test_rename_unambiguates_bare_twin(graph):
    _load("sample", [ToolRecord(name="write_file")])
    _load("sample_v2", [ToolRecord(name="write_file")])
    ingest_governance(Governance(agents=["planner"]))
    assert queries.check_access("planner", "write_file")["verdict"] == "AMBIGUOUS_TOOL"

    loader.retire_unlisted_servers({"sample_v2"})

    # The stale twin is gone; the bare ref resolves to the live tool again.
    assert queries.check_access("planner", "write_file")["verdict"] == "NOT_GRANTED"


def test_reingest_without_tool_collects_retired_governed_ghost(graph):
    """The kept-tool warning's exit path must actually work: once the
    manifest stops mentioning a retired server's governed tool, re-ingest
    deletes the degree-0 ghost — no crawl will ever reconcile that server's
    name again (Codex review of this branch caught it)."""
    _load("sample", [ToolRecord(name="write_file")])
    _load("sample_v2", [ToolRecord(name="write_file")])
    report = ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="legacy-deny", effect="DENY")],
            governed_by={"sample::write_file": ["legacy-deny"]},
        )
    )
    assert report.warnings == []

    retired, warnings = loader.retire_unlisted_servers({"sample_v2"})
    assert retired == ["sample"] and len(warnings) == 1
    # Kept for its DENY — so the bare ref is (honestly) still ambiguous.
    assert queries.check_access("planner", "write_file")["verdict"] == "AMBIGUOUS_TOOL"

    # Operator follows the warning: the manifest stops mentioning the tool.
    report = ingest_governance(
        Governance(agents=["planner"], policies=[Policy(id="legacy-deny", effect="DENY")])
    )
    assert report.warnings == []
    assert queries.check_access("planner", "write_file")["verdict"] == "NOT_GRANTED"


# --- CLI wiring ------------------------------------------------------------


def _write_servers_yaml(tmp_path, servers: list[dict]) -> str:
    path = tmp_path / "servers.yaml"
    path.write_text(yaml.safe_dump({"servers": servers}))
    return str(path)


def test_crawl_with_empty_fleet_retires_stale_server(graph, tmp_path):
    _load("ghost", [ToolRecord(name="haunt")])
    path = _write_servers_yaml(tmp_path, [])

    result = CliRunner().invoke(cli.app, ["crawl", "--servers", path])

    assert result.exit_code == 0
    assert "retired 'ghost'" in result.output
    assert _server_names() == set()


def test_unnamed_failure_skips_prune_and_keeps_graph(graph, tmp_path):
    _load("ghost", [ToolRecord(name="haunt")])
    path = _write_servers_yaml(
        tmp_path,
        [{"transport": "stdio", "command": "sleep", "args": ["30"]}],
    )

    result = CliRunner().invoke(
        cli.app, ["crawl", "--servers", path, "--timeout", "1"]
    )

    assert result.exit_code == 1  # the crawl failure still gates
    assert "fleet reconciliation skipped" in result.output
    assert _server_names() == {"ghost"}


def test_no_prune_flag_preserves_unlisted_servers(graph, tmp_path):
    _load("ghost", [ToolRecord(name="haunt")])
    path = _write_servers_yaml(tmp_path, [])

    result = CliRunner().invoke(cli.app, ["crawl", "--servers", path, "--no-prune"])

    assert result.exit_code == 0
    assert _server_names() == {"ghost"}


def test_duplicate_name_overrides_reject_before_crawl(graph, tmp_path):
    _load("ghost", [ToolRecord(name="haunt")])
    path = _write_servers_yaml(
        tmp_path,
        [
            {"name": "dup", "transport": "stdio", "command": "cmd-a"},
            {"name": "dup", "transport": "stdio", "command": "cmd-b"},
        ],
    )

    result = CliRunner().invoke(cli.app, ["crawl", "--servers", path])

    assert result.exit_code == 1
    # Rejected before connecting to anything — the graph is untouched.
    assert _server_names() == {"ghost"}

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

import random
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from toolgraph import cli
from toolgraph.crawler.client import CatalogBudget
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
        rec = s.run("MATCH (m:GraphMeta {id:'singleton'}) RETURN m.generation AS g").single()
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
    assert blockers == ["stdio:npx"]


def test_label_collision_does_not_protect_unnamed_failure():
    """An unnamed spec whose command equals another spec's name is still an
    unidentifiable failure — blocker detection must ride on the spec, not on
    a display label (Codex review of this branch caught it)."""
    named = ServerSpec(name="npx flaky-server", command="cmd-a")
    unnamed = ServerSpec(command="npx flaky-server")
    results = [CrawlResult(server_name="npx flaky-server", transport="stdio")]
    _, blockers = fleet_keep_names([named, unnamed], results, [(unnamed, "timeout after 1s")])
    assert blockers == ["stdio:npx"]


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


def test_duplicate_checks_boundary_cases():
    """Explicit boundaries the differential test below could sample past."""
    # Empty inputs.
    assert duplicate_name_overrides([]) == set()
    assert duplicate_identities([], []) == set()
    # An empty-string name is falsy: overrides and failures drop it, but a
    # result's server_name is counted as-is. Pinned so a rewrite cannot
    # quietly change which side of that line "" sits on.
    blank = [ServerSpec(name="", command="a"), ServerSpec(name="")]
    assert duplicate_name_overrides(blank) == set()
    assert duplicate_identities([], [(blank[0], "x"), (blank[1], "y")]) == set()
    assert duplicate_identities([CrawlResult(server_name=""), CrawlResult(server_name="")], []) == {
        ""
    }
    # Unnamed specs never collide, with each other or with anything else.
    unnamed = [ServerSpec(command="a"), ServerSpec(command="a")]
    assert duplicate_name_overrides(unnamed) == set()
    assert duplicate_identities([CrawlResult(server_name="a")], [(unnamed[0], "x")]) == set()
    # Result-only and failure-only duplicates.
    assert duplicate_identities(
        [CrawlResult(server_name="r"), CrawlResult(server_name="r")], []
    ) == {"r"}
    assert duplicate_identities([], [(ServerSpec(name="f"), "x"), (ServerSpec(name="f"), "y")]) == {
        "f"
    }
    # Three-way repeat is reported once.
    assert duplicate_name_overrides([ServerSpec(name="t") for _ in range(3)]) == {"t"}


def test_duplicate_checks_match_independent_oracle():
    """Seeded fleets mixing every input class; expected sets are derived from
    the inputs with plain loops, sharing no extraction code with the helpers."""
    rng = random.Random(151)
    pool = ["a", "b", "c", "d", "e", "f", "g", "h", ""]
    for _ in range(200):
        specs = []
        for _ in range(rng.randint(0, 12)):
            roll = rng.random()
            name = None if roll < 0.25 else rng.choice(pool)
            specs.append(ServerSpec(name=name, command="cmd"))
        results = [CrawlResult(server_name=rng.choice(pool)) for _ in range(rng.randint(0, 8))]
        failures = [(s, "boom") for s in specs if rng.random() < 0.5]

        seen_overrides: dict[str, int] = {}
        for spec in specs:
            if spec.name:
                seen_overrides[spec.name] = seen_overrides.get(spec.name, 0) + 1
        expected_overrides = {n for n, c in seen_overrides.items() if c > 1}

        seen_ids: dict[str, int] = {}
        for res in results:
            seen_ids[res.server_name] = seen_ids.get(res.server_name, 0) + 1
        for spec, _ in failures:
            if spec.name:
                seen_ids[spec.name] = seen_ids.get(spec.name, 0) + 1
        expected_ids = {n for n, c in seen_ids.items() if c > 1}

        assert duplicate_name_overrides(specs) == expected_overrides
        assert duplicate_identities(results, failures) == expected_ids


# --- retirement semantics (real graph) ------------------------------------


def test_removed_server_subgraph_is_retired(backend_graph):
    _load("alpha", [ToolRecord(name="read_file")])
    _load("beta", [ToolRecord(name="write_file")])

    retired, warnings = loader.retire_unlisted_servers({"alpha"})

    assert retired == ["beta"]
    assert warnings == []
    assert _server_names() == {"alpha"}
    counts = loader.graph_counts()
    assert counts["tool"] == 1 and counts["exposes"] == 1


def test_retire_is_idempotent_and_noop_keeps_generation(backend_graph):
    _load("alpha", [ToolRecord(name="read_file")])
    _load("beta", [ToolRecord(name="write_file")])
    loader.retire_unlisted_servers({"alpha"})

    gen = _generation()
    retired, warnings = loader.retire_unlisted_servers({"alpha"})

    assert retired == [] and warnings == []
    # ADR-0004: a no-op reconciliation must not invalidate caches.
    assert _generation() == gen


def test_retire_bumps_generation_when_something_changed(backend_graph):
    _load("alpha", [ToolRecord(name="read_file")])
    _load("beta", [ToolRecord(name="write_file")])
    gen = _generation()
    loader.retire_unlisted_servers({"alpha"})
    assert _generation() == gen + 1


def test_governed_tool_kept_drifted_with_hints_cleared(backend_graph):
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


def test_resource_lifecycle_follows_adr_0001(backend_graph):
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


def test_failed_retirement_leaves_the_fleet_in_place(backend_graph, monkeypatch):
    _load("alpha", [ToolRecord(name="read_file")], resources=["file:///shared.csv"])
    _load(
        "beta",
        [
            ToolRecord(name="scratch", read_only_hint=True),
            ToolRecord(name="drop_table", destructive_hint=True),
        ],
        resources=["file:///shared.csv", "file:///b-only.csv"],
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

    def snapshot() -> dict:
        with driver.session() as s:
            tools = sorted(
                (r["key"], r["read_only"], r["destructive"])
                for r in s.run(
                    "MATCH (t:Tool) RETURN t.key AS key, "
                    "t.read_only_hint AS read_only, t.destructive_hint AS destructive"
                )
            )
            uris = sorted(r["u"] for r in s.run("MATCH (r:Resource) RETURN r.uri AS u"))
        return {
            "servers": _server_names(),
            "tools": tools,
            "uris": uris,
            "counts": loader.graph_counts(),
            "state": queries.graph_state(),
            "deny": queries.check_access("planner", "beta::drop_table")["verdict"],
        }

    before = snapshot()
    seen_inside = {}
    original = loader.bump_generation

    def fail(tx):
        rec = tx.run("MATCH (s:MCPServer) RETURN collect(s.name) AS names").single()
        seen_inside["servers"] = sorted(rec["names"])
        raise RuntimeError("injected after mutation")

    monkeypatch.setattr(loader, "bump_generation", fail)
    with pytest.raises(RuntimeError, match="injected"):
        loader.retire_unlisted_servers({"alpha"})
    # Every retirement write ran before the failure: hints cleared, the
    # ungoverned tool, beta and b-only.csv deleted.
    assert seen_inside["servers"] == ["alpha"]
    assert snapshot() == before

    monkeypatch.setattr(loader, "bump_generation", original)
    assert loader.retire_unlisted_servers({"alpha"})[0] == ["beta"]
    after = snapshot()
    assert after["servers"] == {"alpha"}
    assert after["tools"] == [("alpha::read_file", None, None), ("beta::drop_table", None, None)]
    assert after["uris"] == ["file:///shared.csv"]
    assert after["deny"] == "DENY"
    assert after["state"].generation == before["state"].generation + 1


def test_rename_unambiguates_bare_twin(backend_graph):
    _load("sample", [ToolRecord(name="write_file")])
    _load("sample_v2", [ToolRecord(name="write_file")])
    ingest_governance(Governance(agents=["planner"]))
    assert queries.check_access("planner", "write_file")["verdict"] == "AMBIGUOUS_TOOL"

    loader.retire_unlisted_servers({"sample_v2"})

    # The stale twin is gone; the bare ref resolves to the live tool again.
    assert queries.check_access("planner", "write_file")["verdict"] == "NOT_GRANTED"


def test_reingest_without_tool_collects_retired_governed_ghost(backend_graph):
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


def _seed_governed_fleet() -> None:
    """One server, one ungoverned tool, one governed tool behind a DENY."""
    _load("ghost", [ToolRecord(name="haunt"), ToolRecord(name="drop_table")])
    report = ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="prod-deny", effect="DENY")],
            grants=[AccessGrant(agent="planner", tool="ghost::drop_table")],
            governed_by={"ghost::drop_table": ["prod-deny"]},
        )
    )
    assert report.warnings == []
    assert queries.check_access("planner", "ghost::drop_table")["verdict"] == "DENY"


def _snapshot() -> dict:
    state = queries.graph_state()
    return {
        "servers": _server_names(),
        "generation": _generation(),
        "instance": state.instance_id,
        "counts": loader.graph_counts(),
        "deny": queries.check_access("planner", "ghost::drop_table")["verdict"],
    }


_NOT_A_FLEET = {
    "blank": "",
    "comment-only": "# nothing here\n",
    "empty-mapping": "{}\n",
    "missing-key": "other: []\n",
    "null-list": "servers: null\n",
    "yaml-set": "servers: !!set {}\n",
}


@pytest.mark.parametrize("content", list(_NOT_A_FLEET.values()), ids=list(_NOT_A_FLEET))
def test_crawl_rejects_a_file_without_an_explicit_servers_list(
    backend_graph, tmp_path, monkeypatch, content
):
    """ADR-0014: blank, comment-only and ``{}`` used to mean "retire every
    server"; now they exit 1 before any server is contacted and leave the
    graph untouched. ``other: []``, ``servers: null`` and the YAML set are
    in the matrix as already-rejected / newly-strict forms with the same
    no-mutation guarantee."""
    _seed_governed_fleet()
    before = _snapshot()
    path = tmp_path / "servers.yaml"
    path.write_text(content)

    def never(*_a, **_k):
        raise AssertionError("crawl_all must not be reached")

    # The invalid-config handler wraps only load_servers_config, so a sentinel
    # reaching crawl_all would escape as a traceback, not this diagnostic.
    monkeypatch.setattr(cli, "crawl_all", never)
    result = CliRunner().invoke(cli.app, ["crawl", "--servers", str(path)])

    assert result.exit_code == 1
    assert "Invalid servers config" in result.output
    assert "servers" in result.output
    assert _snapshot() == before


def test_crawl_with_empty_fleet_retires_stale_server(backend_graph, tmp_path):
    _seed_governed_fleet()
    path = _write_servers_yaml(tmp_path, [])

    result = CliRunner().invoke(cli.app, ["crawl", "--servers", path])

    assert result.exit_code == 0
    assert "retired 'ghost'" in result.output
    assert _server_names() == set()
    # ADR-0007 retirement rules through the CLI: the governed tool survives,
    # visibly drifted, and its DENY still holds.
    assert [t["tool_key"] for t in queries.drifted_tools()] == ["ghost::drop_table"]
    assert queries.check_access("planner", "ghost::drop_table")["verdict"] == "DENY"


def test_crawl_repeating_an_empty_retirement_keeps_generation(backend_graph, tmp_path):
    _seed_governed_fleet()
    path = _write_servers_yaml(tmp_path, [])
    assert CliRunner().invoke(cli.app, ["crawl", "--servers", path]).exit_code == 0
    generation = _generation()

    result = CliRunner().invoke(cli.app, ["crawl", "--servers", path])

    assert result.exit_code == 0
    assert _server_names() == set()
    assert _generation() == generation


def test_unnamed_failure_skips_prune_and_keeps_graph(backend_graph, tmp_path):
    _load("ghost", [ToolRecord(name="haunt")])
    path = _write_servers_yaml(
        tmp_path,
        [{"transport": "stdio", "command": "sleep", "args": ["30"]}],
    )

    result = CliRunner().invoke(cli.app, ["crawl", "--servers", path, "--timeout", "1"])

    assert result.exit_code == 1  # the crawl failure still gates
    assert "fleet reconciliation skipped" in result.output
    assert _server_names() == {"ghost"}


def test_no_prune_flag_preserves_unlisted_servers(backend_graph, tmp_path):
    _seed_governed_fleet()
    before = _snapshot()
    path = _write_servers_yaml(tmp_path, [])

    result = CliRunner().invoke(cli.app, ["crawl", "--servers", path, "--no-prune"])

    assert result.exit_code == 0
    assert _server_names() == {"ghost"}
    assert queries.drifted_tools() == []
    after = _snapshot()
    assert after["servers"] == before["servers"] and after["counts"] == before["counts"]
    assert after["deny"] == "DENY" and after["instance"] == before["instance"]


def test_duplicate_name_overrides_reject_before_crawl(backend_graph, tmp_path):
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


# --- catalog budget through the CLI (#153, ADR-0016) -----------------------

_SAMPLE = str(Path(__file__).parent / "fixtures" / "sample_server.py")


def _sample_tool_keys() -> set[str]:
    with driver.session() as s:
        return {
            r["k"]
            for r in s.run(
                "MATCH (:MCPServer {name:'sample'})-[:EXPOSES]->(t:Tool) RETURN t.key AS k"
            )
        }


def test_named_server_over_budget_keeps_its_old_graph_state(backend_graph, tmp_path):
    """A budget failure is a crawl failure: the named server is protected, so
    its previous catalog is not reconciled against the incomplete one, and the
    rest of the fleet still prunes."""
    _load("sample", [ToolRecord(name="old_tool")])
    _load("ghost", [ToolRecord(name="haunt")])
    spec = {"name": "sample", "transport": "stdio", "command": sys.executable, "args": [_SAMPLE]}
    path = _write_servers_yaml(tmp_path, [spec])

    result = CliRunner().invoke(cli.app, ["crawl", "--servers", path, "--max-catalog-records", "1"])

    assert result.exit_code == 1
    assert "CatalogBudgetError: catalog exceeds 1 records" in result.output
    assert "ExceptionGroup" not in result.output
    assert _sample_tool_keys() == {"sample::old_tool"}
    assert _server_names() == {"sample"}  # ghost retired, sample protected


def test_unnamed_server_over_budget_skips_the_prune(backend_graph, tmp_path):
    _load("ghost", [ToolRecord(name="haunt")])
    path = _write_servers_yaml(
        tmp_path, [{"transport": "stdio", "command": sys.executable, "args": [_SAMPLE]}]
    )

    result = CliRunner().invoke(cli.app, ["crawl", "--servers", path, "--max-catalog-bytes", "64"])

    assert result.exit_code == 1
    assert "CatalogBudgetError: catalog exceeds 64 bytes" in result.output
    assert "fleet reconciliation skipped" in result.output
    assert _server_names() == {"ghost"}


def test_crawl_forwards_the_budget_options(backend_graph, tmp_path, monkeypatch):
    seen = []

    def fake_crawl_all(specs, timeout, budget):
        seen.append(budget)
        return [], []

    monkeypatch.setattr(cli, "crawl_all", fake_crawl_all)
    path = _write_servers_yaml(tmp_path, [])
    args = ["crawl", "--servers", path, "--max-catalog-bytes", "123", "--max-catalog-records", "4"]

    assert CliRunner().invoke(cli.app, args).exit_code == 0
    assert CliRunner().invoke(cli.app, ["crawl", "--servers", path]).exit_code == 0
    assert seen == [
        CatalogBudget(max_records=4, max_bytes=123),
        CatalogBudget(),
    ]


@pytest.mark.parametrize("option", ["--max-catalog-bytes", "--max-catalog-records"])
def test_crawl_rejects_a_non_positive_budget_before_reading_servers(
    backend_graph, tmp_path, monkeypatch, option
):
    def never(*_a, **_k):
        raise AssertionError("crawl_all must not be reached")

    monkeypatch.setattr(cli, "crawl_all", never)
    result = CliRunner().invoke(
        cli.app, ["crawl", "--servers", str(tmp_path / "missing.yaml"), option, "0"]
    )
    assert result.exit_code == 1
    assert "Invalid catalog budget" in result.output

"""Embedded Ladybug backend conformance smoke tests."""

from __future__ import annotations

import subprocess
import sys

import pytest
from typer.testing import CliRunner

from toolgraph import cli, config
from toolgraph.graph import driver, loader, queries, schema, selector
from toolgraph.manifest.ingest import ingest_governance
from toolgraph.models import (
    AccessGrant,
    CrawlResult,
    DataAccess,
    Governance,
    Policy,
    ResourceRecord,
    ToolRecord,
)
from toolgraph.policy_bundle import build_policy_bundle
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import threading
from toolgraph.manifest.ingest import dry_run_ingest

SECRET = "file:///secret.txt"


@pytest.fixture
def ladybug_graph(tmp_path, monkeypatch):
    pytest.importorskip("ladybug")
    settings = config.Settings(backend="ladybug", db_path=tmp_path / "graph.lbug")
    monkeypatch.setattr(config, "settings", settings)
    driver.close_driver()
    schema.init_schema()
    yield settings
    driver.close_driver()


def _seed() -> None:
    loader.load_crawl_result(
        CrawlResult(
            server_name="alpha",
            tools=[ToolRecord(name="read"), ToolRecord(name="ping")],
            resources=[ResourceRecord(uri=SECRET)],
        )
    )
    report = ingest_governance(
        Governance(
            agents=["codex"],
            policies=[Policy(id="secret-deny", effect="DENY")],
            grants=[
                AccessGrant(agent="codex", tool="alpha::read"),
                AccessGrant(agent="codex", tool="alpha::ping"),
            ],
            data_access=[DataAccess(tool="alpha::read", resource=SECRET, mode="READS")],
            governed_by={SECRET: ["secret-deny"]},
        )
    )
    assert report.applied, report.warnings


def test_ladybug_end_to_end_policy_bundle(ladybug_graph):
    _seed()
    assert queries.check_access("codex", "alpha::read")["verdict"] == "DENY"
    filtered = selector.eligible_tools("codex", ["alpha::ping", "alpha::read"], profile="strict")
    assert filtered["eligible"] == []  # ping is intentionally UNMAPPED
    assert {row["reason"] for row in filtered["rejected"]} == {
        "UNMAPPED",
        "DENY_VIOLATION",
    }
    bundle = build_policy_bundle(agent="codex", profile="strict")
    assert bundle["graph_state"]["instance_id"]
    assert {row["tool_key"] for row in bundle["tools"]} == {
        "alpha::ping",
        "alpha::read",
    }


def test_ladybug_rejected_ingest_preserves_graph_state(ladybug_graph):
    _seed()
    before = queries.graph_state()
    rejected = ingest_governance(
        Governance(
            agents=["codex"],
            grants=[AccessGrant(agent="codex", tool="alpha::missing")],
        )
    )
    assert not rejected.applied
    assert queries.graph_state() == before


def test_ladybug_operational_query_conformance(ladybug_graph):
    _seed()
    unsafe = queries.unsafe_callable_tools("codex")
    assert unsafe and unsafe[0]["classification"] == "violation"
    radius = queries.blast_radius(SECRET)
    assert radius["found"] is True and radius["impacted"]
    assert {row["tool_key"] for row in queries.unmapped_tools()} == {"alpha::ping"}
    assert queries.orphan_policies() == []
    assert queries.unbacked_edges()
    report = queries.audit_report()
    assert report["status"] == "fail"

    retired, warnings = loader.retire_unlisted_servers(set())
    assert retired == ["alpha"] and warnings
    assert {row["tool_key"] for row in queries.drifted_tools()} == {
        "alpha::ping",
        "alpha::read",
    }


def test_ladybug_database_has_one_process_owner(ladybug_graph):
    code = (
        "import ladybug as lb, sys; "
        "db=lb.Database(sys.argv[1]); conn=lb.Connection(db); "
        "conn.execute('RETURN 1 AS ok').get_all()"
    )
    attempted = subprocess.run(
        [sys.executable, "-c", code, str(ladybug_graph.db_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert attempted.returncode != 0
    assert "lock" in (attempted.stdout + attempted.stderr).lower()


def test_init_defaults_to_ladybug_and_persists_runtime_config(tmp_path, monkeypatch):
    pytest.importorskip("ladybug")
    monkeypatch.chdir(tmp_path)
    driver.close_driver()
    result = CliRunner().invoke(cli.app, ["init"])
    try:
        assert result.exit_code == 0, result.output
        saved = config.Settings()
        assert saved.backend == "ladybug"
        assert saved.db_path == tmp_path / ".toolgraph" / "toolgraph.lbug"
        assert (tmp_path / ".toolgraph" / "config.json").exists()
    finally:
        driver.close_driver()


def test_ladybug_reset_changes_instance_id_and_returns_to_generation_zero(
    ladybug_graph,
):
    _seed()
    before = queries.graph_state()
    result = CliRunner().invoke(cli.app, ["reset", "--yes"])
    assert result.exit_code == 0, result.output
    after = queries.graph_state()
    assert after.generation == 0
    assert after.instance_id and after.instance_id != before.instance_id


def test_fresh_embedded_database_reads_as_unversioned(tmp_path, monkeypatch):
    pytest.importorskip("ladybug")
    monkeypatch.setattr(
        config, "settings", config.Settings(backend="ladybug", db_path=tmp_path / "fresh.lbug")
    )
    driver.close_driver()
    try:
        with driver.session() as s:
            assert schema._stored_backend_schema_version(s) is None
    finally:
        driver.close_driver()


@pytest.mark.parametrize(
    "error",
    [
        driver.BackendUnavailableError("BACKEND_UNAVAILABLE: offline"),
        driver.BackendConfigurationError("bad config"),
        RuntimeError("IO exception: corrupted page"),
    ],
)
def test_schema_version_read_only_treats_a_missing_table_as_fresh(error):
    class Failing:
        def run(self, *args, **kwargs):
            raise error

    with pytest.raises(RuntimeError) as raised:
        schema._stored_backend_schema_version(Failing())
    assert raised.value is error


def test_init_schema_does_not_skip_the_version_gate_on_an_outage(monkeypatch):
    statements = []

    class Outage:
        def run(self, query, **kwargs):
            statements.append(query)
            raise driver.BackendUnavailableError("BACKEND_UNAVAILABLE: offline")

    from contextlib import contextmanager

    @contextmanager
    def fake_session():
        yield Outage()

    monkeypatch.setattr(schema, "session", fake_session)

    with pytest.raises(driver.BackendUnavailableError):
        schema.init_schema()
    assert len(statements) == 1  # the version read; no DDL ran


@pytest.mark.parametrize("json_output", [False, True])
def test_uninitialized_ladybug_dry_run_is_controlled_and_unchanged(
    embedded_path, tmp_path, json_output
):
    import ladybug

    embedded_path.parent.mkdir()
    ladybug.Database(embedded_path).close()
    before = hashlib.sha256(embedded_path.read_bytes()).hexdigest()
    gov = tmp_path / "gov.yaml"
    gov.write_text("agents: [bot]\n")
    result = CliRunner().invoke(
        cli.app,
        ["ingest-manifest", "--governance", str(gov), "--dry-run"]
        + (["--json"] if json_output else []),
    )
    assert result.exit_code == 1, result.output
    assert "init-schema" in result.stderr
    assert "Traceback" not in result.output
    assert driver._ladybug_database is None
    assert hashlib.sha256(embedded_path.read_bytes()).hexdigest() == before


def test_owned_readonly_sessions_never_use_writable_acquisition(embedded_path, monkeypatch):
    schema.init_schema()
    assert ingest_governance(Governance(agents=["bot"])).applied
    state = queries.graph_state()
    driver.close_driver()
    before = embedded_path.read_bytes()

    @contextmanager
    def forbidden():
        raise AssertionError("reader reopened a writable database")
        yield  # unreachable; makes forbidden() a generator for @contextmanager

    monkeypatch.setattr(driver, "session", forbidden)
    monkeypatch.setattr(queries, "session", forbidden)
    result = CliRunner().invoke(cli.app, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["healthy"]
    report = dry_run_ingest(Governance())
    assert report.graph_state == {"instance_id": state.instance_id, "generation": state.generation}
    assert report.agents_deleted == ["bot"]
    assert embedded_path.read_bytes() == before
    assert driver._ladybug_database is None


def test_readonly_native_open_cannot_recreate_a_disappearing_file(embedded_path, monkeypatch):
    import ladybug

    schema.init_schema()
    driver.close_driver()
    original = ladybug.Database

    def disappear(path, **kwargs):
        assert kwargs["read_only"] is True
        Path(path).unlink()
        return original(path, **kwargs)

    monkeypatch.setattr(ladybug, "Database", disappear)
    with pytest.raises(driver.BackendUnavailableError):
        with driver.acquire_readonly_session():
            pytest.fail("must refuse missing database")
    assert not embedded_path.exists()


def test_borrowed_readonly_handle_survives_concurrent_shutdown(embedded_path):
    schema.init_schema()
    attempted = threading.Event()
    completed = threading.Event()

    def close():
        attempted.set()
        driver.close_driver()
        completed.set()

    worker = threading.Thread(target=close, daemon=True)
    try:
        with driver.acquire_readonly_session() as session:
            worker.start()
            assert attempted.wait(2)
            assert not completed.wait(0.05)
            assert session.run("RETURN 1 AS ok").single()["ok"] == 1
            with driver.acquire_readonly_session() as nested:
                assert nested.run("RETURN 1 AS ok").single()["ok"] == 1
        worker.join(2)
        assert completed.is_set()
        assert driver._ladybug_database is None
    finally:
        worker.join(2)


@pytest.mark.parametrize("backend", ["neo4j", "ladybug"])
def test_catalog_failure_is_not_reported_as_missing_schema(monkeypatch, backend):
    monkeypatch.setattr(schema, "backend_name", lambda: backend)
    monkeypatch.setattr(
        schema, "_stored_backend_schema_version", lambda s: schema.BACKEND_SCHEMA_VERSION
    )

    class DeniedCatalog:
        def run(self, *_args):
            raise RuntimeError("permission denied")

    with pytest.raises(driver.BackendConfigurationError, match="catalog read permissions") as error:
        schema.check_backend_schema_version(DeniedCatalog())
    assert "init-schema" not in str(error.value)


@pytest.mark.parametrize("native", [False, True])
def test_catalog_outage_preserves_backend_unavailable(monkeypatch, native):
    from neo4j.exceptions import ServiceUnavailable

    monkeypatch.setattr(schema, "backend_name", lambda: "neo4j")
    monkeypatch.setattr(
        schema, "_stored_backend_schema_version", lambda s: schema.BACKEND_SCHEMA_VERSION
    )

    class OfflineCatalog:
        def run(self, *_args):
            raise (ServiceUnavailable if native else driver.BackendUnavailableError)("offline")

    with pytest.raises(driver.BackendUnavailableError):
        schema.check_backend_schema_version(OfflineCatalog())


def test_corrupt_ladybug_dry_run_is_controlled(embedded_path, tmp_path):
    embedded_path.parent.mkdir()
    embedded_path.write_bytes(b"not a database")
    path = tmp_path / "governance.yaml"
    path.write_text("agents: [bot]\n")
    result = CliRunner().invoke(
        cli.app, ["ingest-manifest", "--governance", str(path), "--dry-run"]
    )
    assert result.exit_code == 1
    assert "file integrity and format" in result.stderr
    assert "Traceback" not in result.output
    assert embedded_path.read_bytes() == b"not a database"

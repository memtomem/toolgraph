"""Embedded Ladybug backend conformance smoke tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

from toolgraph import cli, config
from toolgraph.graph import driver, loader, queries, schema, selector
from toolgraph.manifest.ingest import dry_run_ingest, ingest_governance
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

SECRET = "file:///secret.txt"


@pytest.fixture
def ladybug_graph(tmp_path, monkeypatch):
    pytest.importorskip("ladybug")
    settings = config.Settings(backend="ladybug", db_path=tmp_path / "graph.lbug")
    monkeypatch.setitem(vars(config), "settings", settings)
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


def test_init_defaults_to_ladybug_and_persists_runtime_config(
    tmp_path, monkeypatch, fresh_settings
):
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
    monkeypatch.setitem(
        vars(config),
        "settings",
        config.Settings(backend="ladybug", db_path=tmp_path / "fresh.lbug"),
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
    monkeypatch.setattr(driver, "read_session", forbidden)
    monkeypatch.setattr(queries, "read_session", forbidden)
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
    with pytest.raises(driver.BackendUnavailableError), driver.acquire_readonly_session():
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


# Read paths never create a Ladybug database and refuse an uninitialized one
# with one line, instead of a binder traceback after creating the file.

_READ_COMMANDS = [
    ["check-access", "a", "s::t"],
    ["unsafe-tools", "a"],
    ["blast-radius", "file:///x"],
    ["unmapped-tools"],
    ["orphan-policies"],
    ["unbacked-edges"],
    ["drift"],
    ["destructive-unsafeguarded"],
    ["annotation-contradictions"],
    ["audit-report"],
    ["rank-features", "a", "s::t"],
    ["eligible-tools", "a", "s::t"],
    ["selection-explain", "a", "s::t"],
]


def _empty_ladybug_file(path: Path) -> None:
    import ladybug

    path.parent.mkdir(parents=True, exist_ok=True)
    ladybug.Database(path).close()


def _tables(path: Path) -> list[str]:
    import ladybug

    database = ladybug.Database(path, read_only=True)
    try:
        rows = ladybug.Connection(database).execute("CALL show_tables() RETURN *")
        return sorted(row["name"] for row in rows.rows_as_dict().get_all())
    finally:
        database.close()


@pytest.mark.parametrize("argv", _READ_COMMANDS, ids=lambda argv: argv[0])
def test_read_command_on_a_missing_database_creates_nothing(embedded_path, argv):
    pytest.importorskip("ladybug")

    result = CliRunner().invoke(cli.app, argv)

    assert result.exit_code == 1, result.output
    assert result.stderr.splitlines() == [
        f"ERROR: backend unavailable: BACKEND_UNAVAILABLE: Ladybug database {embedded_path} "
        "does not exist or is not a file"
    ]
    assert not embedded_path.parent.exists()


@pytest.mark.parametrize("argv", _READ_COMMANDS, ids=lambda argv: argv[0])
def test_read_command_on_an_uninitialized_database_names_init_schema(embedded_path, argv):
    pytest.importorskip("ladybug")
    _empty_ladybug_file(embedded_path)

    result = CliRunner().invoke(cli.app, argv)

    assert result.exit_code == 1, result.output
    assert result.stderr.splitlines() == [
        "ERROR: backend schema is not initialized or incomplete — run 'toolgraph init-schema'"
    ]
    assert driver._ladybug_database is None
    assert _tables(embedded_path) == []


def test_read_session_is_session_on_neo4j(monkeypatch):
    marker = object()

    @contextmanager
    def fake_session():
        yield marker

    monkeypatch.setitem(vars(config), "settings", config.Settings(backend="neo4j"))
    monkeypatch.setattr(driver, "session", fake_session)

    with driver.read_session() as current:
        assert current is marker


def test_read_checks_a_handle_a_writer_opened_and_keeps_it(embedded_path):
    pytest.importorskip("ladybug")
    with driver.session():
        pass  # a writer created and cached the database without a schema
    writer_handle = driver._ladybug_database

    with pytest.raises(driver.BackendConfigurationError) as raised, driver.read_session():
        pass

    assert raised.value.cause is driver.ConfigurationCause.SCHEMA_NOT_INITIALIZED
    assert driver._ladybug_database is writer_handle


def test_read_after_init_schema_in_the_same_process_succeeds(embedded_path):
    pytest.importorskip("ladybug")
    schema.init_schema()

    assert queries.unmapped_tools(only_granted=False) == []


def test_failed_read_releases_the_database_for_another_process(embedded_path):
    """The serving process must not keep the lock on a database an operator
    has to initialize from a second process."""
    pytest.importorskip("ladybug")
    _empty_ladybug_file(embedded_path)
    with pytest.raises(driver.BackendConfigurationError), driver.read_session():
        pass

    repaired = subprocess.run(
        [sys.executable, "-m", "toolgraph.cli", "init-schema"],
        env={
            "PATH": "",
            "TOOLGRAPH_CONFIG": str(embedded_path.parent / "no-config.json"),
            "TOOLGRAPH_BACKEND": "ladybug",
            "TOOLGRAPH_DB_PATH": str(embedded_path),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert repaired.returncode == 0, repaired.stderr
    assert queries.unmapped_tools(only_granted=False) == []


def test_database_deleted_between_check_and_open_fails_loudly(embedded_path, monkeypatch):
    """Ladybug's writable open has no "must exist" mode, so a file another
    process deletes after the existence check is created again, empty. The
    schema check must then refuse it and drop the handle."""
    pytest.importorskip("ladybug")
    schema.init_schema()
    driver.close_driver()
    original = driver._open_ladybug_database

    def deleted_first(path, **kwargs):
        path.unlink()
        return original(path, **kwargs)

    monkeypatch.setattr(driver, "_open_ladybug_database", deleted_first)

    with pytest.raises(driver.BackendConfigurationError) as raised, driver.read_session():
        pass

    assert raised.value.cause is driver.ConfigurationCause.SCHEMA_NOT_INITIALIZED
    assert driver._ladybug_database is None


def test_failed_read_closes_the_handle_it_opened(embedded_path, monkeypatch):
    """Explicit, not left to garbage collection: an exception traceback that
    outlives the call would otherwise keep the lock."""
    pytest.importorskip("ladybug")
    _empty_ladybug_file(embedded_path)
    opened = []
    original = driver._open_ladybug_database

    def recording(path, **kwargs):
        opened.append(original(path, **kwargs))
        return opened[-1]

    monkeypatch.setattr(driver, "_open_ladybug_database", recording)

    with pytest.raises(driver.BackendConfigurationError), driver.read_session():
        pass

    (handle,) = opened
    assert handle.is_closed


def test_every_read_checks_the_schema_again(embedded_path):
    """Not once per handle: a version changed under a cached handle is
    refused on the next read."""
    pytest.importorskip("ladybug")
    schema.init_schema()
    assert queries.unmapped_tools(only_granted=False) == []
    with driver.session() as s:
        s.run(
            "MATCH (m:GraphMeta {id:'singleton'}) SET m.backend_schema_version = $version",
            version=schema.BACKEND_SCHEMA_VERSION + 1,
        )

    with pytest.raises(driver.BackendConfigurationError) as raised:
        queries.unmapped_tools(only_granted=False)

    assert raised.value.cause is driver.ConfigurationCause.SCHEMA_VERSION_MISMATCH


def test_failed_session_construction_closes_the_handle(embedded_path, monkeypatch):
    pytest.importorskip("ladybug")
    _empty_ladybug_file(embedded_path)
    opened = []
    original = driver._open_ladybug_database

    def recording(path, **kwargs):
        opened.append(original(path, **kwargs))
        return opened[-1]

    def interrupted(database):
        raise KeyboardInterrupt

    monkeypatch.setattr(driver, "_open_ladybug_database", recording)
    monkeypatch.setattr(driver, "LadybugSession", interrupted)

    with pytest.raises(KeyboardInterrupt), driver.read_session():
        pass

    (handle,) = opened
    assert handle.is_closed
    assert driver._ladybug_database is None


def test_writer_without_ladybug_touches_no_directory(embedded_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "ladybug", None)

    with pytest.raises(driver.BackendConfigurationError) as raised, driver.session():
        pass

    assert raised.value.cause is driver.ConfigurationCause.LADYBUG_MISSING
    assert not embedded_path.parent.exists()

"""Backend selection: which database a command runs against, and why."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from neo4j import NotificationClassification
from typer.testing import CliRunner

from toolgraph import cli, config
from toolgraph.cli import app
from toolgraph.graph import driver


def _write_runtime(directory: Path, *, backend: str, db_path: Path) -> Path:
    state = directory / ".toolgraph"
    state.mkdir(parents=True, exist_ok=True)
    path = state / "config.json"
    path.write_text(json.dumps({"schema_version": 1, "backend": backend, "db_path": str(db_path)}))
    return path


@pytest.fixture
def ambient(tmp_path, monkeypatch):
    """A working directory with no ambient settings resolved yet."""
    for name in ("TOOLGRAPH_BACKEND", "TOOLGRAPH_DB_PATH", "TOOLGRAPH_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "_explicit_config_path", None)
    monkeypatch.delitem(vars(config), "settings", raising=False)
    monkeypatch.chdir(tmp_path)
    driver.close_driver()
    yield tmp_path
    driver.close_driver()
    vars(config).pop("settings", None)


def test_env_backend_that_contradicts_the_config_file_is_refused(ambient, monkeypatch):
    runtime = _write_runtime(ambient, backend="ladybug", db_path=ambient / "work.lbug")
    monkeypatch.setenv("TOOLGRAPH_BACKEND", "neo4j")

    result = CliRunner().invoke(cli.app, ["check"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == (
        f"ERROR: conflicting backend configuration ({Path('.toolgraph/config.json')}): "
        "TOOLGRAPH_BACKEND=neo4j but the config file says ladybug. "
        "Pass --config to choose the file, or unset the variable.\n"
    )
    assert runtime.exists()


def test_env_db_path_that_contradicts_the_config_file_is_refused(ambient, monkeypatch):
    _write_runtime(ambient, backend="ladybug", db_path=ambient / "work.lbug")
    monkeypatch.setenv("TOOLGRAPH_DB_PATH", str(ambient / "other.lbug"))

    assert config.ambient_conflict() == (
        f"conflicting backend configuration ({Path('.toolgraph/config.json')}): "
        f"TOOLGRAPH_DB_PATH={ambient / 'other.lbug'} but the config file says "
        f"{ambient / 'work.lbug'}. Pass --config to choose the file, or unset the variable."
    )
    with pytest.raises(config.RuntimeConfigurationError):
        config.settings  # noqa: B018 - building the singleton is the check
    assert not (ambient / "other.lbug").exists()
    assert not (ambient / "work.lbug").exists()


def test_agreeing_sources_and_relative_paths_are_not_a_conflict(ambient, monkeypatch):
    _write_runtime(ambient, backend="ladybug", db_path=Path("work.lbug"))
    monkeypatch.setenv("TOOLGRAPH_BACKEND", "ladybug")
    monkeypatch.setenv("TOOLGRAPH_DB_PATH", str(ambient / "work.lbug"))

    assert config.ambient_conflict() is None
    assert config.settings.backend == "ladybug"


def test_explicit_config_resolves_a_conflict(ambient, monkeypatch):
    pytest.importorskip("ladybug")
    runtime = _write_runtime(ambient, backend="ladybug", db_path=ambient / "work.lbug")
    monkeypatch.setenv("TOOLGRAPH_BACKEND", "neo4j")
    runner = CliRunner()

    created = runner.invoke(cli.app, ["--config", str(runtime), "init-schema"])
    checked = runner.invoke(cli.app, ["--config", str(runtime), "check"])

    assert created.exit_code == 0, created.output
    assert checked.exit_code == 0, checked.output
    assert checked.stdout == f"OK: backend=ladybug path={ambient / 'work.lbug'}\n"


def test_help_and_init_still_work_under_a_conflict(ambient, monkeypatch):
    pytest.importorskip("ladybug")
    _write_runtime(ambient, backend="neo4j", db_path=ambient / "unused.lbug")
    monkeypatch.setenv("TOOLGRAPH_BACKEND", "ladybug")
    runner = CliRunner()

    helped = runner.invoke(cli.app, ["crawl", "--help"])
    assert helped.exit_code == 0, helped.output

    # init is how an operator rewrites the file to resolve the conflict.
    initialized = runner.invoke(cli.app, ["init", "--backend", "ladybug"])
    assert initialized.exit_code == 0, initialized.output
    assert json.loads((ambient / ".toolgraph" / "config.json").read_text())["backend"] == "ladybug"


def test_doctor_reports_where_the_backend_choice_comes_from(ambient, monkeypatch):
    _write_runtime(ambient, backend="ladybug", db_path=ambient / "work.lbug")
    assert config.check_config_health()["backend_source"] == "config-file"
    monkeypatch.setenv("TOOLGRAPH_BACKEND", "ladybug")
    assert config.check_config_health()["backend_source"] == "env"


def _run(code: str, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("TOOLGRAPH_") and k != "PYTHON_DOTENV_DISABLED"
    }
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_importing_the_library_does_not_read_dotenv(tmp_path):
    (tmp_path / ".env").write_text("TOOLGRAPH_BACKEND=from-dotenv\n")

    proc = _run(
        "import os, toolgraph.config, toolgraph.graph.driver; "
        "print(os.environ.get('TOOLGRAPH_BACKEND'))",
        tmp_path,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "None\n"


def test_the_cli_still_reads_dotenv(tmp_path):
    pytest.importorskip("ladybug")
    (tmp_path / ".env").write_text(
        f"TOOLGRAPH_BACKEND=ladybug\nTOOLGRAPH_DB_PATH={tmp_path / 'from-dotenv.lbug'}\n"
    )

    proc = _run("import sys; from toolgraph.cli import app; app()", tmp_path, "init-schema")

    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "from-dotenv.lbug").exists()


@pytest.mark.parametrize(
    ("command", "message"),
    [
        ("crawl", "Invalid servers config at servers.yaml"),
        ("ingest-manifest", "Invalid governance config at governance.yaml"),
    ],
)
def test_env_example_does_not_redirect_omitted_inputs_to_the_examples(tmp_path, command, message):
    root = Path(__file__).resolve().parents[1]
    shutil.copy(root / ".env.example", tmp_path / ".env")
    # Invalid stand-ins: a redirect fails on these before any server or database.
    (tmp_path / "examples").mkdir()
    for name in ("servers.yaml", "governance.yaml"):
        (tmp_path / "examples" / name).write_text("[not a mapping]\n")

    proc = _run("from toolgraph.cli import app; app()", tmp_path, command)

    assert proc.returncode == 1
    assert proc.stderr.startswith(f"{message}: ")


def test_check_does_not_create_a_missing_database(ambient, monkeypatch):
    pytest.importorskip("ladybug")
    missing = ambient / "typo" / "toolgraph.lbug"
    monkeypatch.setattr(config, "settings", config.Settings(backend="ladybug", db_path=missing))

    result = CliRunner().invoke(cli.app, ["check"])

    assert result.exit_code == 1
    assert result.stderr == (
        "ERROR: backend unavailable: BACKEND_UNAVAILABLE: Ladybug database "
        f"{missing} does not exist or is not a file\n"
    )
    assert not missing.parent.exists()


def test_configure_drops_the_connection_to_the_previous_database(ambient):
    pytest.importorskip("ladybug")
    first = _write_runtime(ambient / "a", backend="ladybug", db_path=ambient / "a.lbug")
    second = _write_runtime(ambient / "b", backend="ladybug", db_path=ambient / "b.lbug")

    config.configure(first)
    with driver.session() as s:
        s.run("RETURN 1").single()
    assert driver._ladybug_database is not None

    config.configure(second)

    assert driver._ladybug_database is None
    assert config.settings.db_path == ambient / "b.lbug"


@pytest.mark.parametrize("read_only", [False, True])
def test_a_file_that_is_not_a_database_is_a_configuration_error(ambient, monkeypatch, read_only):
    pytest.importorskip("ladybug")
    path = ambient / "foreign.lbug"
    path.write_bytes(b"not a database" * 100)
    monkeypatch.setattr(config, "settings", config.Settings(backend="ladybug", db_path=path))
    opener = driver.acquire_readonly_session if read_only else driver.session

    with pytest.raises(driver.BackendConfigurationError) as raised, opener() as s:
        s.run("RETURN 1")

    assert not driver.is_backend_unavailable(raised.value)
    assert str(raised.value) == (
        f"BACKEND_CORRUPT: {path} is not a valid Ladybug database file; "
        "check file integrity and format"
    )
    assert path.read_bytes() == b"not a database" * 100


def test_neo4j_read_only_session_rejects_writes(graph):
    from neo4j.exceptions import ClientError

    with pytest.raises(ClientError), driver.acquire_readonly_session() as s:
        s.run("CREATE (:Agent {id: 'should-not-exist'})").consume()

    with driver.session() as s:
        count = s.run("MATCH (a:Agent {id: 'should-not-exist'}) RETURN count(a) AS n").single()["n"]
    assert count == 0


def test_neo4j_does_not_report_names_the_graph_does_not_hold(graph, caplog):
    caplog.set_level(logging.INFO, logger="neo4j.notifications")

    def classifications(query: str) -> list[NotificationClassification]:
        with driver.session() as s:
            summary = s.run(query).consume()
        return [g.classification for g in summary.gql_status_objects if g.is_notification]

    # The server itself leaves these out, not only the log.
    assert classifications("MATCH (n:NoSuchLabel) RETURN n.no_such_property AS x") == []
    assert caplog.records == []

    # Other classifications still arrive: id() is deprecated on Neo4j 5.
    assert classifications("MATCH (n) WHERE id(n) = -1 RETURN n") == [
        NotificationClassification.DEPRECATION
    ]
    assert [(r.levelname, r.args[0].notification.classification) for r in caplog.records] == [
        ("WARNING", NotificationClassification.DEPRECATION)
    ]


@pytest.mark.parametrize("env_value", ["LADYBUG", " ladybug ", "Ladybug"])
def test_backend_names_that_select_the_same_backend_are_not_a_conflict(
    ambient, monkeypatch, env_value
):
    _write_runtime(ambient, backend="ladybug", db_path=ambient / "work.lbug")
    monkeypatch.setenv("TOOLGRAPH_BACKEND", env_value)

    assert config.ambient_conflict() is None


@pytest.mark.parametrize(("env_value", "source"), [(None, "default"), ("neo4j", "env")])
def test_doctor_reports_backend_source_without_a_config_file(
    ambient, monkeypatch, env_value, source
):
    if env_value is not None:
        monkeypatch.setenv("TOOLGRAPH_BACKEND", env_value)

    info = config.check_config_health()

    assert info["exists"] is False
    assert info["backend_source"] == source


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

    # check opens read-only and never creates the database, so make it first.
    created = CliRunner().invoke(app, ["--config", str(runtime), "init-schema"])
    assert created.exit_code == 0, created.output
    result = CliRunner().invoke(app, ["--config", str(runtime), "check"])

    assert result.exit_code == 0, result.output
    assert result.stdout == f"OK: backend=ladybug path={db_path}\n"
    assert not (tmp_path / "shared.lbug").exists()


@pytest.mark.parametrize("mode", ["help", "version", "explicit", "invalid"])
def test_broken_default_configuration_subprocess(tmp_path, mode):
    (tmp_path / ".toolgraph").mkdir()
    broken = tmp_path / ".toolgraph/config.json"
    broken.write_text("{broken")
    valid = tmp_path / "valid.json"
    valid.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "backend": "ladybug",
                "db_path": str(tmp_path / "graph.lbug"),
            }
        )
    )
    env = dict(os.environ, TOOLGRAPH_CONFIG=str(broken))
    commands = {
        "help": ["--help"],
        "version": ["--version"],
        "explicit": ["--config", str(valid), "init-schema"],
        "invalid": ["--config", str(broken), "check"],
    }
    result = subprocess.run(
        [sys.executable, "-c", "from toolgraph.cli import app; app()", *commands[mode]],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == (2 if mode == "invalid" else 0), result.stdout + result.stderr
    assert "Traceback" not in result.stderr

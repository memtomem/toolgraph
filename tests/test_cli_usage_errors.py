"""CLI usage errors and printed paths without credential-shaped input (#206)."""

from __future__ import annotations

import errno
import json
import sys
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from typer.testing import CliRunner

from tests._helpers import flat_output
from toolgraph.cli import app
from toolgraph.error_text import describe_error
from toolgraph.redaction import PATH_WITHHELD, VALUE_WITHHELD, WITHHELD, display_path

ENCODED = "?%74oken=QXZQ"
PLAIN = "password=QXZQ"
# Click and Typer print a typed tab escaped (`\x09`, `\t`), out of the
# scrubber's sight.
TABBED = "password\t=QXZQ"
# ... and other whitespace as `\xa0` or `\u2000` (a repr).
NBSP = "password\u00a0=QXZQ"
EN_QUAD = "password\u2000=QXZQ"


class _OwnOSError(OSError):
    """An OSError subclass: Python keeps it as it is for any errno."""


def _run(args: list[str]):
    return CliRunner().invoke(app, args)


def _assert_hidden(text: str) -> None:
    assert "QX" not in text
    assert "ZQ" not in text


@pytest.mark.parametrize(
    ("args", "shown"),
    [
        ([ENCODED], WITHHELD),
        (["--?%74oken%3DQXZQ"], WITHHELD),
        (["check", "--?%74oken%3DQXZQ"], WITHHELD),
        (["crawl", "--timeout", PLAIN], f"Invalid value for '--timeout': {WITHHELD}"),
        (["crawl", "--max-catalog-bytes", ENCODED], f"'--max-catalog-bytes': {WITHHELD}"),
        (
            ["eligible-tools", "a", "b", "--profile", ENCODED],
            f"Invalid value for '--profile': {WITHHELD}",
        ),
        (
            ["selection-explain", "a", "b", "--profile", PLAIN],
            f"Invalid value for '--profile': {WITHHELD}",
        ),
        (
            ["policy", "propose", "g", "o", "--min-would-block", PLAIN],
            f"Invalid value for '--min-would-block': {WITHHELD}",
        ),
        ([TABBED], WITHHELD),
        (["crawl", "--timeout", TABBED], f"Invalid value for '--timeout': {WITHHELD}"),
        (["check", TABBED], WITHHELD),
        ([NBSP], WITHHELD),
        (["crawl", "--timeout", EN_QUAD], f"Invalid value for '--timeout': {WITHHELD}"),
        (["chek", "--note", PLAIN], WITHHELD),
        (["?%74oken\t=QXZQ"], WITHHELD),
        (["check", "password", "\t=QXZQ"], WITHHELD),
        (
            ["review-candidates", "annotate", "r", "c", "--status", TABBED],
            f"Invalid value for '--status': {WITHHELD}",
        ),
    ],
    ids=[
        "command",
        "root-option",
        "sub-option",
        "sub-float",
        "sub-int",
        "profile-encoded",
        "profile-plain",
        "sub-group-int",
        "escaped-command",
        "escaped-float",
        "escaped-extra-argument",
        "nbsp-command",
        "en-quad-float",
        "unrelated-token",
        "encoded-and-escaped",
        "split-extra-arguments",
        "escaped-choice",
    ],
)
def test_usage_errors_are_scrubbed(args, shown):
    result = _run(args)

    assert result.exit_code == 2
    assert shown in flat_output(result.output)
    _assert_hidden(result.output)


def test_the_cli_catches_the_class_typer_reports_usage_errors_with():
    """``toolgraph.cli`` imports ``ClickException`` from Typer's private
    ``typer._click``. A Typer that moved its engine would still import there
    while raising another class, and every usage error would print unscrubbed;
    the test above shows that, this one names the cause."""
    import typer.core

    from toolgraph import cli

    assert typer.core._click.exceptions.ClickException is cli.ClickException, (
        "Typer no longer catches the ClickException toolgraph.cli imports;"
        " import it from wherever typer.core now takes it"
    )


@pytest.mark.parametrize(
    ("args", "shown"),
    [
        (["chek"], "No such command 'chek'. Did you mean 'check'?"),
        (
            ["eligible-tools", "a", "b", "--profile", "prod"],
            "Invalid value for '--profile': unknown profile 'prod' — expected one of",
        ),
        (
            ["crawl", "--timeout", "soon"],
            "Invalid value for '--timeout': 'soon' is not a valid float.",
        ),
    ],
)
def test_ordinary_usage_errors_are_unchanged(args, shown):
    result = _run(args)

    assert result.exit_code == 2
    assert shown in flat_output(result.output)


def test_an_unknown_profile_keeps_the_valid_profiles_when_its_value_goes():
    """#266: `password:` passes the argv check, but its scrub would run through
    the list of valid profiles; the value goes instead, the list stays."""
    result = _run(["eligible-tools", "a", "b", "--profile", "password:"])

    assert result.exit_code == 2
    shown = (
        f"Invalid value for '--profile': unknown profile {VALUE_WITHHELD} "
        "— expected one of ['explore', 'review', 'strict']"
    )
    # The error box may wrap this line anywhere (#250); the message ends it.
    assert "".join(flat_output(result.output).split()).endswith("".join(shown.split()))


def test_a_usage_error_with_a_credential_shaped_token_is_withheld_whole(tmp_path, fresh_settings):
    """The path is the token: the whole message goes, not only the path."""
    result = _run(["--config", str(tmp_path / ENCODED), "check"])

    assert result.exit_code == 2
    assert f"Invalid value for '--config': {WITHHELD}" in flat_output(result.output)
    _assert_hidden(result.output)


@pytest.mark.parametrize(
    "path",
    ["C:\\bearer\\notes.json", "/srv/tokens/notes.json", "/srv/a\\tb/config.json"],
)
def test_an_ordinary_config_path_is_shown(path, fresh_settings):
    result = _run(["--config", path, "check"])

    assert result.exit_code == 2
    # A relative path is named joined to the working directory, and the error
    # box wraps that line at any character, so the comparison ignores the
    # whitespace a wrap inserts. The paths themselves hold none.
    assert "".join(path.split()) == path
    assert path in "".join(flat_output(result.output).split())


INVALID_SERVERS = "servers:\n  - name: 1\n    bogus: x\n"


def test_crawl_config_error_withholds_the_path(tmp_path):
    path = tmp_path / f"{ENCODED}.yaml"
    path.write_text(INVALID_SERVERS, encoding="utf-8")

    result = _run(["crawl", "--servers", str(path)])

    assert result.exit_code == 1
    assert result.output.startswith(
        f"Invalid servers config at {PATH_WITHHELD}: 2 validation errors for ServersConfig: "
    )
    _assert_hidden(result.output)


def test_governance_config_error_withholds_the_path(tmp_path):
    path = tmp_path / f"{PLAIN}.yaml"
    path.write_text("bogus: x\n", encoding="utf-8")

    result = _run(["ingest-manifest", "--governance", str(path)])

    assert result.exit_code == 1
    assert result.output.startswith(
        f"Invalid governance config at {PATH_WITHHELD}: 1 validation error for Governance: "
    )
    _assert_hidden(result.output)


def test_command_lines_withhold_a_credential_shaped_working_directory(
    tmp_path, monkeypatch, fresh_settings
):
    """Each line that prints a path from the working directory: the runtime
    config and database `init` creates, and the manifests read from there."""
    work = tmp_path / PLAIN
    work.mkdir()
    monkeypatch.chdir(work)
    for name in ("TOOLGRAPH_BACKEND", "TOOLGRAPH_DB_PATH", "TOOLGRAPH_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    (work / "servers.yaml").write_text("servers: []\n", encoding="utf-8")
    (work / "clean.yaml").write_text("agents: [a1]\n", encoding="utf-8")
    (work / "drift.yaml").write_text(
        "agents: [a1]\ngrants: [{agent: a1, tool: 'fs::read', granted_by: ops}]\n",
        encoding="utf-8",
    )

    init = _run(["init", "--backend", "ladybug"])
    check = _run(["check"])
    crawl = _run(["crawl", "--servers", str(work / "servers.yaml")])
    dry_run = _run(["ingest-manifest", "--governance", str(work / "clean.yaml"), "--dry-run"])
    rejected = _run(["ingest-manifest", "--governance", str(work / "drift.yaml"), "--dry-run"])
    refused = _run(["ingest-manifest", "--governance", str(work / "drift.yaml")])
    ingest = _run(["ingest-manifest", "--governance", str(work / "clean.yaml")])

    assert init.output.splitlines()[-1] == (f"Initialized backend=ladybug config={PATH_WITHHELD}")
    assert f"OK: backend=ladybug path={PATH_WITHHELD}" in check.output.splitlines()
    assert crawl.output.splitlines()[0] == f"Crawling 0 server(s) from {PATH_WITHHELD} ..."
    assert dry_run.output.splitlines()[0] == (
        f"DRY-RUN: manifest at {PATH_WITHHELD} would apply cleanly"
    )
    assert rejected.output.splitlines()[0] == (
        f"DRY-RUN REJECTED — manifest at {PATH_WITHHELD} would not apply (graph unchanged)"
    )
    assert refused.output.splitlines()[0] == (
        f"REJECTED — manifest at {PATH_WITHHELD} not applied (graph unchanged)"
    )
    assert f"Ingested governance from {PATH_WITHHELD}" in ingest.output.splitlines()
    for result in (init, check, crawl, dry_run, rejected, refused, ingest):
        _assert_hidden(result.output)


def test_a_malformed_runtime_config_withholds_its_path(tmp_path, monkeypatch, fresh_settings):
    path = tmp_path / PLAIN
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("TOOLGRAPH_CONFIG", str(path))

    result = _run(["check"])

    assert result.exit_code == 1
    assert result.output.startswith(
        f"ERROR: invalid Toolgraph runtime config at {PATH_WITHHELD}: Expecting property name"
    )
    _assert_hidden(result.output)


def test_init_prints_an_os_error_without_its_file_names(tmp_path, fresh_settings):
    """`str(OSError)` repeats the path, escaped (`\\t`) out of the scrubber's sight."""
    blocker = tmp_path / TABBED
    blocker.write_text("x", encoding="utf-8")

    result = _run(["init", "--backend", "ladybug", "--state-dir", str(blocker / "state")])

    assert result.exit_code == 1
    assert result.output == f"ERROR: cannot initialize {PATH_WITHHELD}: Not a directory\n"


def test_example_init_lines_withhold_the_destination(tmp_path):
    result = _run(["example", "init", str(tmp_path / PLAIN)])

    assert result.exit_code == 0
    assert result.output.splitlines()[:2] == [
        f"Created Toolgraph quickstart at {PATH_WITHHELD}",
        f"Next: cd {PATH_WITHHELD} && toolgraph init",
    ]


def test_dry_run_json_refuses_credential_shaped_tool_refs(tmp_path):
    """Item 2 of #206: since identities are refused at load (#220), no dry-run
    warning can carry one, and a resource query is redacted (#218)."""
    for tool in ["fs::?%74oken=QXZQ", "fs::password=QXZQ", "fs::x%3Fpassword%3DQXZQ"]:
        path = tmp_path / "governance.yaml"
        path.write_text(
            f'agents: [a1]\ngrants: [{{agent: a1, tool: "{tool}", granted_by: ops}}]\n',
            encoding="utf-8",
        )

        result = _run(["ingest-manifest", "--governance", str(path), "--dry-run", "--json"])

        assert result.exit_code == 1
        assert "grants.0.tool: tool must not contain credential-shaped data" in result.output
        _assert_hidden(result.output)


def test_dry_run_json_redacts_a_resource_query(tmp_path, monkeypatch, fresh_settings):
    monkeypatch.setenv("TOOLGRAPH_BACKEND", "ladybug")
    monkeypatch.setenv("TOOLGRAPH_DB_PATH", str(tmp_path / "graph.lbug"))
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "governance.yaml"
    path.write_text(
        "agents: [a1]\n"
        "policies: [{id: p1, effect: DENY}]\n"
        'governed_by: {"file:///x?token=QXZQ": [p1]}\n',
        encoding="utf-8",
    )
    assert _run(["init"]).exit_code == 0

    result = _run(["ingest-manifest", "--governance", str(path), "--dry-run", "--json"])

    delta = json.loads(result.output)
    assert delta["warnings"] == []
    _assert_hidden(result.output)


@pytest.mark.parametrize(
    ("path", "shown"),
    [
        (Path("/srv/toolgraph/servers.yaml"), "/srv/toolgraph/servers.yaml"),
        (Path("/tmp/password=QXZQ.yaml"), PATH_WITHHELD),
        (Path("/tmp/?%74oken=QXZQ"), PATH_WITHHELD),
        ("neo4j://u:QXZQ@h/x", PATH_WITHHELD),
    ],
)
def test_display_path_shows_a_path_whole_or_not_at_all(path, shown):
    assert display_path(path) == shown


def test_ordinary_text_still_goes_through_the_scrubber():
    """No token is credential-shaped, so the message is scrubbed, not withheld."""
    result = _run(["crawl", "--timeout", "token:QXZQ"])

    assert result.exit_code == 2
    assert "Invalid value for '--timeout': 'token=<redacted> is not a valid float." in flat_output(
        result.output
    )
    _assert_hidden(result.output)


@pytest.mark.parametrize("directory", [TABBED, NBSP, EN_QUAD])
@pytest.mark.parametrize(
    ("args", "prefix"),
    [
        (["crawl", "--servers"], "Invalid servers config at"),
        (["ingest-manifest", "--governance"], "Invalid governance config at"),
    ],
)
def test_a_missing_config_file_names_neither_path(tmp_path, directory, args, prefix):
    """`str(OSError)` writes the file name with repr, escaped out of the scrubber's sight."""
    missing = tmp_path / directory / "config.yaml"
    (tmp_path / directory).mkdir()

    result = _run([*args, str(missing)])

    assert result.exit_code == 1
    assert result.output == (
        f"{prefix} {PATH_WITHHELD}: [Errno 2] No such file or directory: '{PATH_WITHHELD}'\n"
    )


@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError(2, "No such file or directory", "/srv/servers.yaml"),
        OSError(18, "Invalid cross-device link", "/srv/a", None, "/mnt/b"),
        PermissionError(13, "Permission denied"),
        OSError(2, None, "/srv/servers.yaml"),
        OSError(2, "", "/srv/servers.yaml"),
        OSError(None, None, "/srv/servers.yaml"),
        OSError(None, "", "/srv/servers.yaml"),
        OSError(9, "Bad file descriptor", 3),
        OSError("no errno"),
        _OwnOSError(errno.EAGAIN, "busy", 3),
        _OwnOSError(errno.EAGAIN, "busy", 10**100),
        OSError(2, "No such file or directory", Path("/srv/servers.yaml")),
        OSError(2, "No such file or directory", b"/srv/servers.yaml"),
        HTTPError("/srv/config.yaml", 404, "Not Found", Message(), None),
    ],
)
def test_an_os_error_reads_as_python_prints_it(exc):
    """An ordinary file name, of any type, reads exactly as Python prints it.

    A copy built with the name as a constructor argument made an ``EAGAIN``
    error a ``BlockingIOError``, which reads an integer there as
    ``characters_written``: the name went missing, or the copy raised."""
    assert describe_error(exc) == str(exc)


@pytest.mark.skipif(sys.platform != "win32", reason="winerror exists on Windows only")
def test_a_windows_error_keeps_its_code():
    exc = OSError(0, "The system cannot find the path specified", "/srv/a", 3)

    assert describe_error(exc) == str(exc)


@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError(2, "No such file or directory", f"/srv/{TABBED}".encode()),
        OSError(18, "Invalid cross-device link", "/srv/a", None, f"/srv/{NBSP}".encode()),
        OSError(2, None, f"/srv/{TABBED}"),
        OSError(2, "", f"/srv/{TABBED}"),
        OSError(None, None, f"/srv/{TABBED}"),
        OSError(2, "No such file or directory", Path(f"/srv/{TABBED}")),
    ],
)
def test_a_credential_shaped_file_name_is_withheld(exc):
    _assert_hidden(describe_error(exc))
    assert PATH_WITHHELD in describe_error(exc)


@pytest.mark.parametrize("marker", [PLAIN, ENCODED, TABBED])
def test_yaml_and_export_and_health_and_driver_paths_withhold_only_the_path(
    tmp_path, monkeypatch, fresh_settings, marker
):
    from toolgraph import config, policy_propose, yaml_loader
    from toolgraph.graph import driver

    folder = tmp_path / marker
    folder.mkdir()

    yaml_path = folder / "config.yaml"
    yaml_path.write_text("- not-a-mapping\n", encoding="utf-8")
    with pytest.raises(ValueError) as yaml_exc:
        yaml_loader.load_yaml_mapping(yaml_path)
    assert str(yaml_exc.value) == f"expected a YAML mapping at document root: {PATH_WITHHELD}"
    _assert_hidden(str(yaml_exc.value))

    export_path = folder / "observed.jsonl"
    export_path.write_text('{"envelope": 1}\n', encoding="utf-8")
    with pytest.raises(policy_propose.ProvenanceError) as exp_exc:
        policy_propose.load_export(export_path)
    assert str(exp_exc.value) == f"{PATH_WITHHELD}:1: envelope must be a JSON object"
    _assert_hidden(str(exp_exc.value))

    monkeypatch.setenv("TOOLGRAPH_CONFIG", str(folder / "missing.json"))
    health = config.check_config_health()
    assert health["path"] == PATH_WITHHELD
    assert health["detail"] == f"configuration file not found at {PATH_WITHHELD}"
    _assert_hidden(json.dumps(health))

    corrupt_err = driver._not_a_database(folder / "graph.lbug")
    missing_err = driver._missing_database(folder / "graph.lbug")
    assert describe_error(corrupt_err) == (
        f"BACKEND_CORRUPT: {PATH_WITHHELD} is not a valid Ladybug database file; "
        "check file integrity and format"
    )
    assert describe_error(missing_err) == (
        f"BACKEND_UNAVAILABLE: Ladybug database {PATH_WITHHELD} does not exist or is not a file"
    )


def _ladybug_raising(message: str, *, before=lambda: None):
    """A stand-in `ladybug` module whose every open raises RuntimeError(message)."""

    def database(*args, **kwargs):
        before()
        raise RuntimeError(message)

    return SimpleNamespace(Database=database)


@pytest.mark.parametrize("marker", [PLAIN, ENCODED, TABBED])
def test_limit_row_digest_and_open_errors_withhold_only_the_path(
    tmp_path, monkeypatch, fresh_settings, marker
):
    from toolgraph import config, policy_propose, yaml_loader
    from toolgraph.graph import driver
    from toolgraph.manifest.parser import governance_digest, load_governance

    folder = tmp_path / marker
    folder.mkdir()

    governance = folder / "governance.yaml"
    governance.write_text(
        "agents: [ops-agent]\n"
        "grants:\n"
        "  - {agent: ops-agent, tool: filesystem::read_file, granted_by: security-team}\n",
        encoding="utf-8",
    )
    bare = folder / "bare.jsonl"
    bare.write_text(
        json.dumps({"agent": "ops-agent", "decision": "eligible", "tool_key": "a::b"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(policy_propose.ProvenanceError) as raised:
        policy_propose.run_propose(governance, bare)
    digest = governance_digest(load_governance(governance))
    hints = [line for line in str(raised.value).splitlines() if line.startswith("The digest")]
    assert hints == [f"The digest for {PATH_WITHHELD} right now is {digest}."]
    _assert_hidden(str(raised.value))

    not_json = folder / "not-json.jsonl"
    not_json.write_text("{\n", encoding="utf-8")
    with pytest.raises(ValueError) as raised:
        policy_propose.load_export(not_json)
    assert str(raised.value) == (
        f"{PATH_WITHHELD}:1: not valid JSON (Expecting property name enclosed in double quotes)"
    )
    _assert_hidden(str(raised.value))

    monkeypatch.setattr(policy_propose, "MAX_EXPORT_BYTES", 4)
    with pytest.raises(policy_propose.PolicyProposeError) as raised:
        policy_propose.load_export(bare)
    assert str(raised.value) == f"refusing to load {PATH_WITHHELD}: exceeds the 4-byte limit"

    monkeypatch.setattr(yaml_loader, "MAX_YAML_BYTES", 4)
    with pytest.raises(ValueError) as raised:
        yaml_loader.load_yaml_mapping(governance)
    assert str(raised.value) == f"refusing to load {PATH_WITHHELD}: exceeds the 4-byte limit"

    database = folder / "graph.lbug"
    database.write_bytes(b"")
    locked = f"BACKEND_LOCKED: Ladybug database {PATH_WITHHELD} already has an owner"
    config.settings = config.Settings(backend="ladybug", db_path=database)
    monkeypatch.setattr(driver, "_ladybug_database", None)
    monkeypatch.setitem(sys.modules, "ladybug", _ladybug_raising("Could not set lock on file"))
    with pytest.raises(driver.BackendLockedError) as raised:
        driver._open_ladybug_database(database, create=False)
    assert describe_error(raised.value) == locked
    with pytest.raises(driver.BackendLockedError) as raised, driver.acquire_readonly_session():
        pass
    assert describe_error(raised.value) == locked

    monkeypatch.setitem(sys.modules, "ladybug", _ladybug_raising("IO", before=database.unlink))
    with pytest.raises(driver.BackendUnavailableError) as raised, driver.acquire_readonly_session():
        pass
    assert describe_error(raised.value) == (
        f"BACKEND_UNAVAILABLE: Ladybug database {PATH_WITHHELD} does not exist"
    )

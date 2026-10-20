"""CLI usage errors and printed paths without credential-shaped input (#206)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from toolgraph.cli import app
from toolgraph.error_text import describe_error
from toolgraph.redaction import PATH_WITHHELD, WITHHELD, display_path

ENCODED = "?%74oken=QXZQ"
PLAIN = "password=QXZQ"
# Click and Typer print a typed tab escaped (`\x09`, `\t`), out of the
# scrubber's sight.
TABBED = "password\t=QXZQ"
# ... and other whitespace as `\xa0` or `\u2000` (a repr).
NBSP = "password\u00a0=QXZQ"
EN_QUAD = "password\u2000=QXZQ"


def _run(args: list[str]):
    return CliRunner().invoke(app, args)


def _flat(output: str) -> str:
    """Typer's error box wraps long lines; join it back into one line."""
    lines = [line.strip("│╭╮╰╯─ ") for line in output.splitlines()]
    return " ".join(line for line in lines if line)


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
        (["eligible-tools", "a", "b", "--profile", ENCODED], f"Invalid value: {WITHHELD}"),
        (["selection-explain", "a", "b", "--profile", PLAIN], f"Invalid value: {WITHHELD}"),
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
        "escaped-choice",
        "nbsp-command",
        "en-quad-float",
        "unrelated-token",
        "encoded-and-escaped",
    ],
)
def test_usage_errors_are_scrubbed(args, shown):
    result = _run(args)

    assert result.exit_code == 2
    assert shown in _flat(result.output)
    _assert_hidden(result.output)


@pytest.mark.parametrize(
    ("args", "shown"),
    [
        (["chek"], "No such command 'chek'. Did you mean 'check'?"),
        (
            ["eligible-tools", "a", "b", "--profile", "prod"],
            "Invalid value: unknown profile 'prod' — expected one of",
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
    assert shown in _flat(result.output)


def test_a_usage_error_with_a_credential_shaped_token_is_withheld_whole(tmp_path, fresh_settings):
    """The path is the token: the whole message goes, not only the path."""
    result = _run(["--config", str(tmp_path / ENCODED), "check"])

    assert result.exit_code == 2
    assert f"Invalid value for --config: {WITHHELD}" in _flat(result.output)
    _assert_hidden(result.output)


@pytest.mark.parametrize(
    "path",
    ["C:\\bearer\\notes.json", "/srv/tokens/notes.json", "/srv/a\\tb/config.json"],
)
def test_an_ordinary_config_path_is_shown(path, fresh_settings):
    result = _run(["--config", path, "check"])

    assert result.exit_code == 2
    assert path in _flat(result.output)


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
    assert "Invalid value for '--timeout': 'token=<redacted> is not a valid float." in _flat(
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
    ],
)
def test_an_os_error_reads_as_python_prints_it(exc):
    assert describe_error(exc) == str(exc)


@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError(2, "No such file or directory", f"/srv/{TABBED}".encode()),
        OSError(18, "Invalid cross-device link", "/srv/a", None, f"/srv/{NBSP}".encode()),
    ],
)
def test_a_bytes_file_name_is_decoded_before_it_is_shown(exc):
    _assert_hidden(describe_error(exc))
    assert PATH_WITHHELD in describe_error(exc)

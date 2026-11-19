"""A stdio server's working directory, ``cwd:`` in servers.yaml (#275)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from toolgraph import config
from toolgraph.cli import app
from toolgraph.crawler import transports
from toolgraph.crawler.crawl import load_servers_config, resolve_cwd
from toolgraph.errors import ValueFreeError
from toolgraph.graph import driver, schema
from toolgraph.models import ServerSpec


def _servers_file(directory: Path, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "servers.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_a_relative_cwd_names_a_directory_next_to_the_servers_file(tmp_path, monkeypatch):
    absolute = tmp_path / "elsewhere"
    _servers_file(
        tmp_path / "config",
        "servers:\n"
        "  - {name: rel, command: python3, cwd: srv}\n"
        "  - {name: dot, command: python3, cwd: .}\n"
        f"  - {{name: abs, command: python3, cwd: {json.dumps(str(absolute))}}}\n"
        "  - {name: unset, command: python3}\n",
    )
    # The crawl is started from somewhere else, with a relative --servers path.
    monkeypatch.chdir(tmp_path)

    specs = {spec.name: spec for spec in load_servers_config(Path("config/servers.yaml"))}

    config_dir = (tmp_path / "config").resolve()
    assert specs["rel"].cwd == str(config_dir / "srv")
    assert specs["dot"].cwd == str(config_dir)
    assert specs["abs"].cwd == str(absolute)
    assert specs["unset"].cwd is None


@pytest.mark.parametrize(
    ("cwd", "expected"),
    [
        ("srv", r"D:\config\srv"),
        (".", r"D:\config"),
        (r"C:\abs", r"C:\abs"),
        # Rooted without a drive: the servers file's drive, not process state.
        (r"\top", r"D:\top"),
    ],
)
def test_windows_cwd_values_resolve_against_the_servers_file(cwd, expected):
    assert resolve_cwd(PureWindowsPath(r"D:\config"), cwd) == expected


@pytest.mark.parametrize("base", [r"D:\config", r"C:\config"], ids=["other-drive", "same-drive"])
@pytest.mark.parametrize("cwd", ["C:foo", "C:"])
def test_a_drive_relative_cwd_is_refused(base, cwd):
    """``C:foo`` is relative to drive C's current directory, which servers.yaml cannot name.

    On the servers file's own drive the join would yield ``C:\\config\\foo`` and
    hide it, so the check reads the value before joining.
    """
    with pytest.raises(ValueFreeError) as raised:
        resolve_cwd(PureWindowsPath(base), cwd)
    assert str(raised.value) == (
        "cwd must be an absolute path or a path relative to the servers file, "
        "not relative to another drive"
    )
    assert cwd not in str(raised.value)


def test_a_posix_name_that_looks_like_a_drive_is_a_plain_directory():
    assert resolve_cwd(PurePosixPath("/config"), "C:foo") == "/config/C:foo"


def test_a_symlinked_servers_file_resolves_cwd_where_it_was_named(tmp_path):
    shared = _servers_file(
        tmp_path / "shared", "servers:\n  - {name: s, command: python3, cwd: .}\n"
    )
    (tmp_path / "demo").mkdir()
    link = tmp_path / "demo" / "servers.yaml"
    try:
        link.symlink_to(shared)
    except OSError as exc:  # Windows without the symlink privilege
        pytest.skip(f"cannot create a symlink here: {exc}")

    (spec,) = load_servers_config(link)

    assert spec.cwd == str((tmp_path / "demo").resolve())


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (
            {"name": "h", "transport": "streamable-http", "url": "http://h/mcp", "cwd": "x"},
            "cwd is only valid for stdio servers",
        ),
        ({"name": "s", "transport": "sse", "url": "http://h/sse", "cwd": "x"}, "only valid"),
        ({"name": "e", "command": "python3", "cwd": " "}, "cwd must not be empty"),
    ],
)
def test_cwd_is_refused_where_it_cannot_apply(spec, message):
    with pytest.raises(ValidationError, match=message):
        ServerSpec.model_validate(spec)


@pytest.mark.parametrize("cwd", [None, "/srv/mcp"])
def test_the_stdio_transport_starts_the_server_in_cwd(monkeypatch, cwd):
    seen = []

    @asynccontextmanager
    async def fake_stdio_client(params):
        seen.append(params)
        raise RuntimeError("stop after capturing the parameters")
        yield  # pragma: no cover

    monkeypatch.setattr(transports, "stdio_client", fake_stdio_client)

    async def open_once():
        async with transports.open_session(ServerSpec(command="python3", cwd=cwd)):
            pass  # pragma: no cover

    with pytest.raises(RuntimeError, match="stop after"):
        asyncio.run(open_once())
    assert len(seen) == 1
    assert seen[0].cwd == cwd


def test_the_quickstart_crawls_from_outside_its_directory(tmp_path, monkeypatch):
    """The #275 reproduction: ``crawl --servers demo/servers.yaml`` from the parent."""
    pytest.importorskip("ladybug")
    monkeypatch.setitem(
        vars(config),
        "settings",
        config.Settings(backend="ladybug", db_path=tmp_path / "graph.lbug"),
    )
    driver.close_driver()
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    try:
        created = runner.invoke(app, ["example", "init", "demo"])
        assert created.exit_code == 0, created.output
        schema.init_schema()

        crawled = runner.invoke(app, ["crawl", "--servers", "demo/servers.yaml"])

        assert crawled.exit_code == 0, crawled.output
        assert "[ok] policy-gateway (2 tools, 1 resources)" in crawled.stdout
        assert "[error]" not in crawled.output
        # The directory is a launch detail, not part of the server's identity:
        # nothing persisted names it.
        with driver.session() as s:
            endpoints = [
                r["endpoint"] for r in s.run("MATCH (m:MCPServer) RETURN m.endpoint AS endpoint")
            ]
        assert len(endpoints) == 1
        assert str(tmp_path) not in endpoints[0]
        assert "demo" not in endpoints[0]
    finally:
        driver.close_driver()

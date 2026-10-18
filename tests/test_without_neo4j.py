"""ADR-0017 step 2: Ladybug never imports ``neo4j``, and Neo4j without the extra is one error.

The dev group installs ``neo4j`` through ``testcontainers[neo4j]``, so an ordinary
test cannot tell whether a Ladybug path imported it. These tests run in child
processes with a ``sys.meta_path`` finder that records every attempt to import
``neo4j`` and then refuses it. Each attempt is appended to a file as it happens,
so a child that is killed instead of exiting still reports. A caught
``ImportError`` leaves nothing in ``sys.modules``, so the recorded attempts are
the evidence, not the module table.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from toolgraph.crawler.transports import open_session
from toolgraph.models import ServerSpec

# Installed before anything else in the child. The self-check runs before
# recording starts: it proves the finder refuses neo4j, so zero recorded
# attempts cannot come from a blocker that never engaged.
BLOCKER = textwrap.dedent(
    """
    import os, sys

    _ATTEMPTS = os.environ["TOOLGRAPH_TEST_NEO4J_ATTEMPTS"]
    _recording = False

    class _BlockNeo4j:
        def find_spec(self, name, path=None, target=None):
            if name == "neo4j" or name.startswith("neo4j."):
                if _recording:
                    with open(_ATTEMPTS, "a") as out:
                        out.write(name + "\\n")
                raise ImportError(f"blocked for the test: {name}")
            return None

    sys.meta_path.insert(0, _BlockNeo4j())
    try:
        import neo4j  # noqa: F401
    except ImportError:
        pass
    else:
        raise SystemExit("blocker inactive: neo4j imported")
    _recording = True
    with open(os.environ["TOOLGRAPH_TEST_BLOCKER_ACTIVE"], "w") as out:
        out.write("active")
    """
)

LADYBUG_FLOW = textwrap.dedent(
    """
    import subprocess, sys
    from pathlib import Path

    import ladybug
    import yaml
    from typer.testing import CliRunner

    from toolgraph import config
    from toolgraph.cli import app
    from toolgraph.graph import driver, loader, queries, schema
    from toolgraph.health import run_doctor
    from toolgraph.models import CrawlResult, ResourceRecord, ToolRecord

    work = Path.cwd()
    runner = CliRunner()

    def cli(*args, code=0):
        result = runner.invoke(app, list(args))
        assert result.exit_code == code, (args, result.exit_code, result.output)
        return result

    def fails(error, fn):
        try:
            fn()
        except error as exc:
            return exc
        raise AssertionError(f"expected {error.__name__}")

    def use(path):
        driver.close_driver()
        config.settings = config.Settings(backend="ladybug", db_path=path)

    # Success paths: write, read, compile, diagnose.
    use(work / "graph.lbug")
    schema.init_schema()
    loader.load_crawl_result(
        CrawlResult(
            server_name="s",
            transport="stdio",
            tools=[ToolRecord(name="t", destructive_hint=True)],
            resources=[ResourceRecord(uri="file:///x")],
        )
    )
    (work / "governance.yaml").write_text(yaml.safe_dump({
        "agents": ["a"],
        "policies": [{"id": "p", "effect": "DENY"}],
        "grants": [{"agent": "a", "tool": "s::t"}],
        "data_access": [{"tool": "s::t", "resource": "file:///x", "mode": "READS"}],
        "governed_by": {"file:///x": ["p"]},
    }))
    cli("ingest-manifest", "--governance", str(work / "governance.yaml"))
    cli("check-access", "a", "t")
    cli("audit-report")
    cli("policy", "compile", "--agent", "a", "--profile", "review",
        "--output", str(work / "bundle.json"))
    cli("check")
    assert run_doctor().healthy
    assert schema.inspect_backend_schema()["healthy"]
    driver.close_driver()
    with driver.acquire_readonly_session() as s:
        assert s.run("MATCH (t:Tool) RETURN count(t) AS n").single()["n"] == 1
    assert queries.graph_state().generation > 0

    # Failure paths: a failing query, an exception unwinding out of a session,
    # a corrupt file, a missing file read-only, a database another process owns.
    def failing_query():
        with driver.session() as s:
            s.run("MATCH (n:NoSuchTable) RETURN n")
    fails(RuntimeError, failing_query)

    def unwinding():
        with driver.session():
            raise KeyError("unwind")
    fails(KeyError, unwinding)

    (work / "corrupt.lbug").write_bytes(b"not a database" * 100)
    use(work / "corrupt.lbug")
    assert "BACKEND_CORRUPT" in str(fails(driver.BackendConfigurationError, schema.init_schema))

    use(work / "absent.lbug")
    def read_absent():
        with driver.acquire_readonly_session():
            pass
    fails(driver.BackendUnavailableError, read_absent)

    owner = subprocess.Popen(
        [sys.executable, "-c",
         "import ladybug, sys, time; db = ladybug.Database(sys.argv[1]); "
         "print('ready', flush=True); time.sleep(60)",
         str(work / "graph.lbug")],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert owner.stdout.readline().strip() == "ready"
        use(work / "graph.lbug")
        fails(driver.BackendLockedError, schema.init_schema)
    finally:
        owner.kill()
        owner.wait()
    """
)

NEO4J_WITHOUT_EXTRA = textwrap.dedent(
    """
    from toolgraph import config
    from toolgraph.graph import driver

    config.settings = config.Settings(backend="neo4j")
    for entry in (
        driver.verify_connectivity,
        lambda: driver.session().__enter__(),
        lambda: driver.acquire_readonly_session().__enter__(),
    ):
        try:
            entry()
        except driver.BackendConfigurationError as exc:
            assert "toolgraph[neo4j]" in str(exc), exc
        else:
            raise AssertionError("expected the missing-extra error")
    """
)


def _child_env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TOOLGRAPH_", "NEO4J_"))}
    env.update(
        TOOLGRAPH_CONFIG=str(tmp_path / "no-config.json"),
        TOOLGRAPH_TEST_NEO4J_ATTEMPTS=str(tmp_path / "attempts.txt"),
        TOOLGRAPH_TEST_BLOCKER_ACTIVE=str(tmp_path / "active.txt"),
        PYTHON_DOTENV_DISABLED="1",
    )
    return env


def _attempts(tmp_path: Path) -> list[str]:
    assert (tmp_path / "active.txt").read_text() == "active"
    path = tmp_path / "attempts.txt"
    return path.read_text().splitlines() if path.exists() else []


def _run(tmp_path: Path, script: str, env: dict[str, str] | None = None):
    return subprocess.run(
        [sys.executable, "-c", BLOCKER + script],
        cwd=tmp_path,
        env=env or _child_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_ladybug_paths_never_import_neo4j(tmp_path):
    result = _run(tmp_path, LADYBUG_FLOW)
    assert result.returncode == 0, result.stderr
    assert _attempts(tmp_path) == []


def test_neo4j_without_the_extra_names_it_at_every_entry(tmp_path):
    result = _run(tmp_path, NEO4J_WITHOUT_EXTRA)
    assert result.returncode == 0, result.stderr
    assert _attempts(tmp_path)  # the error comes from the guarded import, not a guess


def test_cli_reports_the_missing_extra_in_one_line(tmp_path):
    env = _child_env(tmp_path) | {"TOOLGRAPH_BACKEND": "neo4j"}
    script = "import sys; sys.argv = ['toolgraph', 'check']; from toolgraph.cli import app; app()"
    result = _run(tmp_path, script, env)
    assert result.returncode == 1
    assert result.stderr.splitlines() == [
        "ERROR: backend=neo4j requires the 'neo4j' extra: "
        "install with `uv sync --extra neo4j` or `pip install toolgraph[neo4j]`"
    ]


async def test_mcp_server_on_ladybug_never_imports_neo4j(tmp_path):
    db_path = tmp_path / "graph.lbug"
    seed = _run(
        tmp_path,
        "from toolgraph import config\n"
        "from toolgraph.graph import driver, schema\n"
        f"config.settings = config.Settings(backend='ladybug', db_path={str(db_path)!r})\n"
        "schema.init_schema()\n"
        "driver.close_driver()\n",
    )
    assert seed.returncode == 0, seed.stderr
    assert _attempts(tmp_path) == []
    # The server child must write its own marker, or an inactive blocker passes.
    (tmp_path / "active.txt").unlink()

    env = _child_env(tmp_path) | {"TOOLGRAPH_BACKEND": "ladybug", "TOOLGRAPH_DB_PATH": str(db_path)}
    spec = ServerSpec(
        name="toolgraph",
        transport="stdio",
        command=sys.executable,
        args=[
            "-c",
            BLOCKER + "import runpy; runpy.run_module('toolgraph.server.app', run_name='__main__')",
        ],
        env=env,
    )
    async with open_session(spec) as session:
        await session.initialize()
        result = await session.call_tool("unmapped_tools", {})
    got = result.structured_content or json.loads(result.content[0].text)
    assert got["count"] == 0
    assert got["graph_state_verified"] is True
    assert _attempts(tmp_path) == []

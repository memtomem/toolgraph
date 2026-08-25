"""CLI backend-outage contract: redacted single-line ERROR, never a traceback.

The MCP surface already types outages as a redacted ``backend_unavailable``
envelope (test_server_errors.py); these tests pin the same guarantee for the
CLI, where an unhandled ``BackendUnavailableError`` used to escape as a raw
traceback carrying the driver's connection URI.
"""

from __future__ import annotations

import errno
import os
import stat
import warnings

import pytest
from typer.testing import CliRunner

from toolgraph import cli
from toolgraph.artifacts import ArtifactDurabilityWarning, atomic_write_private
from toolgraph.graph.driver import BackendUnavailableError

# A message shaped like the neo4j driver's: carries the full connection URI
# with userinfo. The CLI must never print the credential part back.
OUTAGE = BackendUnavailableError(
    "Couldn't connect to bolt://neo4j:supersecret@graph.internal:7687"
)


def _raise_outage(*args, **kwargs):
    raise OUTAGE


def _all_output(result) -> str:
    """stdout+stderr across click versions (older click mixes them)."""
    try:
        return result.output + result.stderr
    except ValueError:  # click < 8.2 with mixed streams
        return result.output


@pytest.mark.parametrize(
    ("argv", "attr"),
    [
        (["check"], ("toolgraph.cli.driver.verify_connectivity",)),
        (["init-schema"], ("toolgraph.cli.schema.init_schema",)),
        (["drift"], ("toolgraph.cli.queries.drifted_tools",)),
        (["audit-report"], ("toolgraph.cli.queries.audit_report",)),
        (["check-access", "planner", "sample::read_file"],
         ("toolgraph.cli.queries.check_access",)),
        (["unsafe-tools", "planner"], ("toolgraph.cli.queries.agent_exists",)),
        (["rank-features", "planner", "sample::read_file"],
         ("toolgraph.cli.queries.agent_exists",)),
        (["blast-radius", "file:///data/customers.csv"],
         ("toolgraph.cli.queries.blast_radius",)),
    ],
)
def test_backend_outage_is_one_redacted_error_line(monkeypatch, argv, attr):
    for target in attr:
        monkeypatch.setattr(target, _raise_outage)
    result = CliRunner().invoke(cli.app, argv)

    output = _all_output(result)
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in output
    assert "backend unavailable" in output
    assert "supersecret" not in output


def test_policy_compile_outage_is_redacted(monkeypatch, tmp_path):
    monkeypatch.setattr("toolgraph.policy_bundle.queries.with_graph_state", _raise_outage)
    result = CliRunner().invoke(
        cli.app,
        ["policy", "compile", "--agent", "planner",
         "--output", str(tmp_path / "bundle.json")],
    )

    output = _all_output(result)
    assert result.exit_code == 1
    assert "Traceback" not in output
    assert "supersecret" not in output


@pytest.mark.skipif(os.name == "nt", reason="directory fsync is POSIX-only")
def test_atomic_write_warns_but_publishes_when_directory_fsync_fails(
    monkeypatch, tmp_path
):
    """A dir-fsync I/O failure after os.replace must not fail the write."""
    real_fsync = os.fsync

    def failing_directory_fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(5, "injected directory I/O failure")
        return real_fsync(descriptor)

    monkeypatch.setattr("toolgraph.artifacts.os.fsync", failing_directory_fsync)
    target = tmp_path / "artifact.json"
    with pytest.warns(ArtifactDurabilityWarning, match="crash durability"):
        digest = atomic_write_private(target, b"{}\n")

    assert target.read_bytes() == b"{}\n"
    assert len(digest) == 64


@pytest.mark.skipif(os.name == "nt", reason="directory fsync is POSIX-only")
def test_atomic_write_tolerates_unsupported_directory_fsync(monkeypatch, tmp_path):
    """EINVAL-style unsupported dir fsync stays silent (network mounts)."""
    real_fsync = os.fsync

    def unsupported_directory_fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "directory fsync unsupported")
        return real_fsync(descriptor)

    monkeypatch.setattr("toolgraph.artifacts.os.fsync", unsupported_directory_fsync)
    target = tmp_path / "artifact.json"
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        digest = atomic_write_private(target, b"{}\n")

    assert target.read_bytes() == b"{}\n"
    assert len(digest) == 64

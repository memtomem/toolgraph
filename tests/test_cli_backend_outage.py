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
from types import SimpleNamespace

import pytest
from neo4j._sync.io._bolt import Bolt
from neo4j.exceptions import AuthError, ClientError, ConfigurationError, Neo4jError
from typer.testing import CliRunner

from toolgraph import cli, config
from toolgraph.artifacts import ArtifactDurabilityWarning, atomic_write_private
from toolgraph.graph import driver as driver_mod
from toolgraph.graph import schema as schema_mod
from toolgraph.graph.driver import (
    BackendConfigurationError,
    BackendUnavailableError,
    is_backend_unavailable,
)

# A message shaped like the neo4j driver's: carries the full connection URI
# with userinfo. The CLI must never print the credential part back.
OUTAGE = BackendUnavailableError("Couldn't connect to bolt://neo4j:supersecret@graph.internal:7687")


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
        (
            ["audit-report"],
            ("toolgraph.cli.queries.graph_state", "toolgraph.cli.queries.audit_report"),
        ),
        (["check-access", "planner", "sample::read_file"], ("toolgraph.cli.queries.check_access",)),
        (["unsafe-tools", "planner"], ("toolgraph.cli.queries.agent_exists",)),
        (
            ["rank-features", "planner", "sample::read_file"],
            ("toolgraph.cli.queries.agent_exists",),
        ),
        (["blast-radius", "file:///data/customers.csv"], ("toolgraph.cli.queries.blast_radius",)),
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
        ["policy", "compile", "--agent", "planner", "--output", str(tmp_path / "bundle.json")],
    )

    output = _all_output(result)
    assert result.exit_code == 1
    assert "Traceback" not in output
    assert "supersecret" not in output


@pytest.mark.skipif(os.name == "nt", reason="directory fsync is POSIX-only")
def test_atomic_write_warns_but_publishes_when_directory_fsync_fails(monkeypatch, tmp_path):
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


# --- rejected Neo4j credentials (#133) -------------------------------------
#
# The native error is raised BELOW the driver seam (from a fake neo4j driver
# and session), so these tests exercise the translation itself rather than
# injecting an already-typed error.


FAKE_PASSWORD = "hunter2-not-real"

_AUTH_CODES = [
    "Neo.ClientError.Security.Unauthorized",
    "Neo.ClientError.Security.TokenExpired",
    "Neo.ClientError.Security.CredentialsExpired",
    "Neo.ClientError.Security.AuthenticationRateLimit",
    # Not in Neo4j's current status-code list, but neo4j 6.4 still hydrates
    # it as AuthError, so it takes the same path.
    "Neo.ClientError.Security.AuthorizationFailed",
]


def _native(code: str) -> Neo4jError:
    """A driver error hydrated the way neo4j 6.x builds it from a server reply.

    The message carries a fake password, so a test can prove the CLI never
    echoes the driver's text.
    """
    return Neo4jError._hydrate_neo4j(
        code=code, message=f"auth failed for neo4j/{FAKE_PASSWORD} at bolt://graph:7687"
    )


class _FailingSession:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, *_a, **_k):
        raise self._error


class _FailingDriver:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def verify_connectivity(self):
        raise self._error

    def session(self, **_kw):
        return _FailingSession(self._error)


@pytest.fixture
def neo4j_failing_with(monkeypatch):
    def install(error: Exception) -> None:
        monkeypatch.setitem(vars(config), "settings", config.Settings(backend="neo4j"))
        monkeypatch.setattr(driver_mod, "backend_name", lambda: "neo4j")
        monkeypatch.setattr(driver_mod, "get_driver", lambda: _FailingDriver(error))

    return install


@pytest.mark.parametrize("code", _AUTH_CODES)
@pytest.mark.parametrize("argv", [["check"], ["drift"]], ids=["connectivity", "mid-query"])
def test_rejected_credentials_are_one_error_line(neo4j_failing_with, code, argv):
    neo4j_failing_with(_native(code))

    result = CliRunner().invoke(cli.app, argv)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.splitlines() == [
        f"ERROR: Neo4j refused authentication ({code}); check NEO4J_USER, NEO4J_PASSWORD "
        "and the account's auth provider"
    ]
    assert "Traceback" not in _all_output(result)
    assert FAKE_PASSWORD not in _all_output(result)


@pytest.mark.parametrize("code", _AUTH_CODES)
def test_seam_types_rejected_credentials_as_non_retryable_configuration(neo4j_failing_with, code):
    native = _native(code)
    neo4j_failing_with(native)

    with (
        pytest.raises(BackendConfigurationError) as info,
        driver_mod.acquire_readonly_session() as current,
    ):
        current.run("RETURN 1")

    assert info.value.__cause__ is native
    assert not is_backend_unavailable(info.value)
    assert FAKE_PASSWORD not in str(info.value)


@pytest.mark.parametrize(
    "code",
    ["Neo.ClientError.Statement.SyntaxError", "Neo.ClientError.Security.Forbidden"],
)
def test_other_client_errors_pass_the_seam_untouched(neo4j_failing_with, code):
    native = _native(code)
    assert isinstance(native, ClientError) and not isinstance(native, AuthError)
    neo4j_failing_with(native)

    with pytest.raises(ClientError) as info, driver_mod.session() as current:
        current.run("RETURN 1")

    assert info.value is native


def _too_old_server() -> ConfigurationError:
    """The error neo4j 6.x raises on a Bolt 4.4 connection asked to filter
    notifications, produced by the driver's own check so its text stays real."""
    connection = SimpleNamespace(
        supports_notification_filtering=False,
        PROTOCOL_VERSION=(4, 4),
        server_info=SimpleNamespace(agent="Neo4j/4.4.48"),
    )
    try:
        Bolt.assert_notification_filtering_support(connection)
    except ConfigurationError as exc:
        return exc
    raise AssertionError("the driver accepted notification filtering on Bolt 4.4")


@pytest.mark.parametrize("argv", [["check"], ["drift"]], ids=["connectivity", "mid-query"])
def test_server_older_than_the_minimum_is_one_error_line(neo4j_failing_with, argv):
    neo4j_failing_with(_too_old_server())

    result = CliRunner().invoke(cli.app, argv)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.splitlines() == [
        "ERROR: Neo4j server is older than 5.7, the minimum Toolgraph supports; upgrade the server"
    ]


def test_seam_types_a_server_older_than_the_minimum_as_configuration(neo4j_failing_with):
    native = _too_old_server()
    neo4j_failing_with(native)

    with pytest.raises(BackendConfigurationError) as info, driver_mod.session() as current:
        current.run("RETURN 1")

    assert info.value.__cause__ is native
    assert not is_backend_unavailable(info.value)


@pytest.mark.parametrize(
    "message",
    [
        "User switching is not supported for Bolt Protocol 4.4.",
        # neo4j 6.4's refusal over the Query API, whatever the server version.
        "Notification filtering is not supported via the Query API/HTTP.",
    ],
)
def test_other_driver_configuration_errors_pass_the_seam_untouched(neo4j_failing_with, message):
    native = ConfigurationError(message)
    neo4j_failing_with(native)

    with pytest.raises(ConfigurationError) as info, driver_mod.session() as current:
        current.run("RETURN 1")

    assert info.value is native


class _CatalogRejects:
    """Version read succeeds; the constraint catalog read is rejected."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def run(self, query, *_a, **_k):
        if "SHOW CONSTRAINTS" in query:
            raise self._error

        class _Row:
            def single(self):
                return {"version": schema_mod.BACKEND_SCHEMA_VERSION}

        return _Row()


def test_schema_inspection_keeps_the_credentials_diagnostic(monkeypatch):
    """The catalog read has a catch-all that rewords failures as a catalog
    permission problem; a rejected credential must keep its own message."""
    monkeypatch.setattr(schema_mod, "backend_name", lambda: "neo4j")
    code = "Neo.ClientError.Security.CredentialsExpired"

    with pytest.raises(BackendConfigurationError, match="Neo4j refused authentication"):
        schema_mod.inspect_backend_schema(_CatalogRejects(_native(code)))

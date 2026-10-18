"""Typed MCP availability errors without weakening contract failures."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import pytest
from mcp.types import CallToolRequestParams, CallToolResult
from neo4j.exceptions import (
    ClientError,
    ConnectionAcquisitionTimeoutError,
    DatabaseUnavailable,
    ServiceUnavailable,
    SessionExpired,
)

from toolgraph import config
from toolgraph.errors import ContractError
from toolgraph.graph import driver
from toolgraph.graph.driver import (
    BackendConfigurationError,
    BackendLockedError,
    BackendUnavailableError,
    ConfigurationCause,
    is_backend_unavailable,
)
from toolgraph.server import app

_BACKEND_PAYLOAD = {
    "error_kind": "backend_unavailable",
    "retryable": True,
    "message": "Toolgraph backend is temporarily unavailable; retry later.",
}

_TOOLS = [
    ("check_access", {"agent": "agent", "tool": "server::tool"}),
    ("unsafe_callable_tools", {"agent": "agent"}),
    ("blast_radius", {"node": "file:///data"}),
    ("unmapped_tools", {}),
    ("orphan_policies", {}),
    ("unbacked_edges", {}),
    ("drifted_tools", {}),
    ("destructive_unsafeguarded", {}),
    ("annotation_contradictions", {}),
    ("audit_report", {}),
    ("rank_features", {"agent": "agent", "candidates": []}),
    ("eligible_tools", {"agent": "agent", "candidates": []}),
    ("selection_explain", {"agent": "agent", "tool": "server::tool"}),
]


async def _wire_call(name: str, arguments: dict) -> CallToolResult:
    """Drive the tool through the same handler the transport would call.

    Going through the handler rather than ``call_tool`` is the point: the
    ToolError-to-``is_error`` conversion lives in the handler, so a test that
    called the tool directly would never see the envelope this file exists to
    pin. In mcp 2.x handlers are keyed by method name and take the request
    context first; toolgraph's ``call_tool`` ignores that context, so a
    placeholder is enough to reach the envelope.
    """
    entry = app.mcp._lowlevel_server.get_request_handler("tools/call")
    assert entry is not None, "the server registered no tools/call handler"
    result = await entry.handler(None, CallToolRequestParams(name=name, arguments=arguments))
    assert isinstance(result, CallToolResult)
    return result


def test_tool_fixtures_cover_every_registered_tool():
    """Fail loudly when a tool is added without an availability fixture."""
    registered = {tool.name for tool in asyncio.run(app.mcp.list_tools())}
    assert registered == {name for name, _ in _TOOLS}


@pytest.mark.parametrize(
    "exc",
    [
        BackendUnavailableError("offline"),
        BackendLockedError("locked"),
    ],
)
def test_backend_unavailable_classifier_accepts_only_typed_outages(exc):
    assert is_backend_unavailable(exc)


@pytest.mark.parametrize(
    "native",
    [
        ServiceUnavailable("offline"),
        SessionExpired("expired"),
        ConnectionAcquisitionTimeoutError("timed out"),
        DatabaseUnavailable("unavailable"),
    ],
)
def test_native_outages_are_typed_at_the_driver_seam(native):
    """The narrow native taxonomy becomes BackendUnavailableError at the seam."""
    with pytest.raises(BackendUnavailableError) as raised, driver._translating_backend_errors():
        raise native
    assert raised.value.__cause__ is native
    assert is_backend_unavailable(raised.value)


def test_outage_thrown_through_session_yield_is_typed(monkeypatch):
    """The seam must cover mid-query failures, not just session acquisition.

    The exception is raised inside the caller's ``with`` body, so Python throws
    it back into the generator — the case a try/except around acquisition alone
    would miss.
    """
    native = ServiceUnavailable("bolt down mid-query")

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def run(self, *args, **kwargs):
            raise native

    class _Driver:
        def session(self, **kwargs):
            return _Session()

    monkeypatch.setitem(vars(config), "settings", config.Settings(backend="neo4j"))
    monkeypatch.setattr(driver, "get_driver", lambda: _Driver())
    monkeypatch.setattr(driver, "backend_name", lambda: "neo4j")

    with pytest.raises(BackendUnavailableError) as raised, driver.session() as current:
        current.run("MATCH (n) RETURN n")
    assert raised.value.__cause__ is native


def test_client_error_through_session_yield_stays_loud(monkeypatch):
    """A Cypher/contract error must not be laundered into an outage."""

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def run(self, *args, **kwargs):
            raise ClientError("syntax error")

    class _Driver:
        def session(self, **kwargs):
            return _Session()

    monkeypatch.setitem(vars(config), "settings", config.Settings(backend="neo4j"))
    monkeypatch.setattr(driver, "get_driver", lambda: _Driver())
    monkeypatch.setattr(driver, "backend_name", lambda: "neo4j")

    with pytest.raises(ClientError) as raised, driver.session() as current:
        current.run("BAD")
    assert not is_backend_unavailable(raised.value)


def test_untyped_native_outage_is_not_classified():
    """A raw driver exception means it escaped the seam — stay loud."""
    assert not is_backend_unavailable(ServiceUnavailable("offline"))


@pytest.mark.parametrize(
    "exc",
    [
        BackendConfigurationError("bad config"),
        ClientError("bad query"),
        ValueError("bad profile"),
        RuntimeError("bug"),
    ],
)
def test_backend_unavailable_classifier_rejects_contract_and_internal_errors(exc):
    assert not is_backend_unavailable(exc)


def test_backend_unavailable_classifier_walks_wrapped_causes():
    inner = BackendUnavailableError("offline")
    outer = RuntimeError("framework wrapper")
    outer.__cause__ = inner
    assert is_backend_unavailable(outer)


def test_backend_unavailable_classifier_respects_suppressed_context():
    """``raise ... from None`` means the author chose the outer error."""
    try:
        try:
            raise BackendUnavailableError("offline")
        except BackendUnavailableError:
            raise RuntimeError("cleanup bug") from None
    except RuntimeError as exc:
        assert not is_backend_unavailable(exc)


def test_backend_unavailable_classifier_follows_implicit_context():
    try:
        try:
            raise BackendUnavailableError("offline")
        except BackendUnavailableError:
            raise RuntimeError("cleanup bug")  # noqa: B904 - the implicit __context__ is under test
    except RuntimeError as exc:
        assert is_backend_unavailable(exc)


@pytest.mark.parametrize("name,arguments", _TOOLS)
async def test_every_mcp_tool_returns_typed_backend_unavailable(monkeypatch, name, arguments):
    def unavailable(_fetch):
        raise BackendUnavailableError(
            "bolt://alice:secret@example.test password=hunter2 sentinel-detail"
        )

    monkeypatch.setattr(app, "_with_generation", unavailable)
    result = await _wire_call(name, arguments)

    assert result.is_error is True
    assert result.structured_content == _BACKEND_PAYLOAD
    assert result.content[0].text == _BACKEND_PAYLOAD["message"]
    encoded = result.model_dump_json()
    assert "alice" not in encoded
    assert "hunter2" not in encoded
    assert "sentinel-detail" not in encoded
    assert "graph_generation" not in encoded


async def test_untyped_native_outage_still_carries_no_credentials(monkeypatch):
    """A driver exception that never passed the seam must not leak on the wire.

    The classifier only recognizes ``BackendUnavailableError``, so a native
    outage raised outside the seam falls through to the unstructured error
    path. That path is the one a future call site added outside ``session()``
    would take, and driver text can embed connection credentials.
    """

    def unavailable(_fetch):
        raise ServiceUnavailable(
            "bolt://alice:secret@example.test password=hunter2 sentinel-detail"
        )

    monkeypatch.setattr(app, "_with_generation", unavailable)
    result = await _wire_call("check_access", {"agent": "a", "tool": "s::t"})

    assert result.is_error is True
    encoded = result.model_dump_json()
    assert "alice" not in encoded
    assert "secret" not in encoded
    assert "hunter2" not in encoded


def test_retryable_tracks_the_advertised_annotations():
    """Retry safety must match what the server actually advertises.

    Stated as an equivalence rather than "everything is read-only" so adding a
    genuine write tool later does not make this test wrong.
    """
    tools = asyncio.run(app.mcp.list_tools())
    for tool in tools:
        declared = bool(tool.annotations and tool.annotations.read_only_hint)
        assert asyncio.run(app.mcp._is_retryable(tool.name)) is declared


def test_unknown_tool_is_not_retryable():
    assert asyncio.run(app.mcp._is_retryable("no_such_tool")) is False


async def test_outage_envelope_survives_a_failing_tool_listing(monkeypatch):
    """A retry-safety lookup fault must not destroy the outage envelope.

    The listing runs while an outage is already being reported; if it escaped,
    the caller would get an unstructured internal error precisely when the
    typed availability signal matters most.
    """

    def unavailable(_fetch):
        raise BackendUnavailableError("bolt://alice:hunter2@example.test")

    async def broken_list_tools():
        raise RuntimeError("schema failure sentinel")

    monkeypatch.setattr(app, "_with_generation", unavailable)
    monkeypatch.setattr(app.mcp, "list_tools", broken_list_tools)
    result = await _wire_call("check_access", {"agent": "a", "tool": "s::t"})

    assert result.is_error is True
    assert result.structured_content == {**_BACKEND_PAYLOAD, "retryable": False}
    encoded = result.model_dump_json()
    assert "schema failure sentinel" not in encoded
    assert "hunter2" not in encoded


async def test_outage_envelope_survives_a_faulty_listed_tool(monkeypatch):
    """The guard covers iteration, not just the listing call itself."""

    class _FaultyTool:
        @property
        def name(self):
            raise RuntimeError("attribute sentinel")

    async def faulty_list_tools():
        return [_FaultyTool()]

    monkeypatch.setattr(
        app,
        "_with_generation",
        lambda _fetch: (_ for _ in ()).throw(BackendUnavailableError("down")),
    )
    monkeypatch.setattr(app.mcp, "list_tools", faulty_list_tools)
    result = await _wire_call("check_access", {"agent": "a", "tool": "s::t"})

    assert result.structured_content == {**_BACKEND_PAYLOAD, "retryable": False}
    assert "attribute sentinel" not in result.model_dump_json()


async def test_cancellation_during_retry_lookup_propagates(monkeypatch):
    """Cancellation must not be absorbed into a response."""

    async def cancelled_list_tools():
        raise asyncio.CancelledError

    monkeypatch.setattr(app.mcp, "list_tools", cancelled_list_tools)
    with pytest.raises(asyncio.CancelledError):
        await app.mcp._is_retryable("check_access")


def test_current_tools_are_all_read_only_graph_reads():
    """Today every shipped tool is a pure read; flag any future change here."""
    for tool in asyncio.run(app.mcp.list_tools()):
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.idempotent_hint is True


def test_each_tool_owns_its_annotations_object():
    """Shared annotations would let one tool's edit rewrite every tool's."""
    tools = asyncio.run(app.mcp.list_tools())
    assert len({id(tool.annotations) for tool in tools}) == len(tools)


def test_registered_write_tool_never_claims_retryable():
    """A real non-read-only registration, on an isolated server instance."""
    server = app._ToolgraphMCP("probe")

    @server.read_only_tool()
    def reader() -> dict:
        return {}

    @server.tool()
    def writer() -> dict:
        return {}

    assert asyncio.run(server._is_retryable("reader")) is True
    assert asyncio.run(server._is_retryable("writer")) is False


def test_explicit_tool_name_keeps_retry_safety():
    """A ``name=`` override must not silently drop retry safety."""
    server = app._ToolgraphMCP("probe")

    @server.read_only_tool(name="aliased")
    def original_name() -> dict:
        return {}

    registered = {tool.name for tool in asyncio.run(server.list_tools())}
    assert registered == {"aliased"}
    assert asyncio.run(server._is_retryable("aliased")) is True
    assert asyncio.run(server._is_retryable("original_name")) is False


def test_duplicate_name_cannot_confer_retry_safety_on_a_write_tool():
    """MCPServer keeps the FIRST tool on a duplicate name.

    A later read-only registration under the same name is discarded, so the
    retained write tool must not inherit the discarded tool's retry claim.
    """
    server = app._ToolgraphMCP("probe")

    @server.tool(name="same")
    def writer() -> dict:
        return {}

    @server.read_only_tool(name="same")
    def reader() -> dict:
        return {}

    (retained,) = asyncio.run(server.list_tools())
    assert not (retained.annotations and retained.annotations.read_only_hint)
    assert asyncio.run(server._is_retryable("same")) is False


def test_read_only_tool_rejects_conflicting_annotations():
    """The helper owns annotations; a caller override would be silently lost."""
    server = app._ToolgraphMCP("probe")
    with pytest.raises(TypeError):
        server.read_only_tool(annotations=object())


async def test_contract_error_keeps_existing_unstructured_error(monkeypatch):
    """A ContractError says what the caller passed, so it is worth forwarding."""

    def invalid(_fetch):
        raise ContractError("unknown profile 'production'")

    monkeypatch.setattr(app, "_with_generation", invalid)
    result = await _wire_call(
        "eligible_tools",
        {"agent": "agent", "candidates": [], "profile": "production"},
    )

    assert result.is_error is True
    assert result.structured_content is None
    assert "unknown profile" in result.content[0].text


@pytest.mark.parametrize(
    "exc",
    [
        PermissionError(13, "Permission denied", "/srv/private/customer-x/db.lbug"),
        RuntimeError("MATCH (t:Tool) RETURN t.internal_note AS SENTINEL_LEAK"),
        RuntimeError("auth=('alice', 'SENTINEL_LEAK')"),
        RuntimeError("credential='SENTINEL_LEAK'"),
        RuntimeError("password='harmless SENTINEL_LEAK'"),
    ],
    ids=["fs-path", "cypher", "auth-tuple", "credential-label", "spaced-password"],
)
async def test_unexpected_exception_text_never_reaches_the_caller(monkeypatch, exc):
    """Everything that is not a ContractError keeps mcp 2.x's mask.

    `redact_text` models URLs and a few credential labels; it does not model
    filesystem paths, query bodies, an `auth=(user, secret)` tuple, the label
    `credential`, or a quoted secret containing a space. Forwarding arbitrary
    exception text after scrubbing therefore leaks, which is why only errors
    this repo raises on purpose are forwarded.
    """

    def boom(_fetch):
        raise exc

    monkeypatch.setattr(app, "_with_generation", boom)
    result = await _wire_call("eligible_tools", {"agent": "agent", "candidates": []})

    assert result.is_error is True
    text = result.content[0].text
    assert "SENTINEL_LEAK" not in text
    assert "/srv/private" not in text
    assert "internal_note" not in text


async def test_success_wire_shape_is_unchanged(monkeypatch):
    monkeypatch.setattr(app, "_with_generation", lambda _fetch: {"ok": True})
    result = await _wire_call("check_access", {"agent": "agent", "tool": "server::tool"})

    assert result.is_error is False
    assert result.structured_content is None
    assert json.loads(result.content[0].text) == {"ok": True}


# A tagged configuration error's text can name a path or a configured value;
# every misconfiguration test raises one carrying these and checks the wire.
_OPERATOR_DETAIL = "BACKEND_CORRUPT: /srv/private/customer-x/SENTINEL_LEAK.lbug"


def _assert_no_operator_detail(result: CallToolResult) -> None:
    encoded = result.model_dump_json()
    assert "SENTINEL_LEAK" not in encoded
    assert "/srv/private" not in encoded


def _misconfigured(cause: ConfigurationCause) -> dict:
    return {
        "error_kind": "backend_misconfigured",
        "retryable": False,
        "cause": cause.value,
        "message": app._BACKEND_MISCONFIGURED_MESSAGES[cause],
    }


def _raising(exc_factory):
    def fetch(_fetch):
        raise exc_factory()

    return fetch


def test_every_cause_has_a_fixed_message():
    assert set(app._BACKEND_MISCONFIGURED_MESSAGES) == set(ConfigurationCause)


@pytest.mark.parametrize("name,arguments", _TOOLS)
async def test_every_mcp_tool_returns_typed_backend_misconfigured(monkeypatch, name, arguments):
    cause = ConfigurationCause.BACKEND_CORRUPT
    monkeypatch.setattr(
        app,
        "_with_generation",
        _raising(lambda: BackendConfigurationError(_OPERATOR_DETAIL, cause=cause)),
    )
    result = await _wire_call(name, arguments)

    assert result.is_error is True
    assert result.structured_content == _misconfigured(cause)
    assert result.content[0].text == app._BACKEND_MISCONFIGURED_MESSAGES[cause]
    _assert_no_operator_detail(result)


@pytest.mark.parametrize("cause", list(ConfigurationCause))
async def test_each_cause_reaches_the_caller_by_name(monkeypatch, cause):
    monkeypatch.setattr(
        app,
        "_with_generation",
        _raising(lambda: BackendConfigurationError(_OPERATOR_DETAIL, cause=cause)),
    )
    result = await _wire_call("check_access", {"agent": "a", "tool": "s::t"})

    assert result.structured_content == _misconfigured(cause)
    _assert_no_operator_detail(result)


async def test_untagged_configuration_error_keeps_the_mask(monkeypatch):
    monkeypatch.setattr(
        app, "_with_generation", _raising(lambda: BackendConfigurationError(_OPERATOR_DETAIL))
    )
    result = await _wire_call("check_access", {"agent": "a", "tool": "s::t"})

    assert result.is_error is True
    assert result.structured_content is None
    assert result.content[0].text == "Error executing tool check_access"
    _assert_no_operator_detail(result)


def _config_wrapping_outage():
    try:
        raise BackendUnavailableError("offline")
    except BackendUnavailableError as exc:
        return _chained(
            BackendConfigurationError(_OPERATOR_DETAIL, cause=ConfigurationCause.BACKEND_CORRUPT),
            exc,
        )


def _outage_wrapping_config():
    return _chained(
        BackendUnavailableError("offline"),
        BackendConfigurationError(_OPERATOR_DETAIL, cause=ConfigurationCause.BACKEND_CORRUPT),
    )


def _outage_in_implicit_context_of_config():
    try:
        try:
            raise BackendConfigurationError(
                _OPERATOR_DETAIL, cause=ConfigurationCause.BACKEND_CORRUPT
            )
        except BackendConfigurationError:
            raise BackendUnavailableError("offline")  # noqa: B904 - implicit __context__ under test
    except BackendUnavailableError as exc:
        return exc


def _chained(outer: BaseException, cause: BaseException) -> BaseException:
    outer.__cause__ = cause
    return outer


@pytest.mark.parametrize(
    "factory",
    [_config_wrapping_outage, _outage_wrapping_config, _outage_in_implicit_context_of_config],
    ids=["config-from-outage", "outage-from-config", "outage-during-config"],
)
async def test_misconfiguration_wins_over_an_outage_in_the_same_chain(monkeypatch, factory):
    """A false "retryable" sends a consumer into a retry loop; a false
    "not retryable" only fails loud. Configuration therefore wins."""
    monkeypatch.setattr(app, "_with_generation", _raising(factory))
    result = await _wire_call("check_access", {"agent": "a", "tool": "s::t"})

    assert result.structured_content == _misconfigured(ConfigurationCause.BACKEND_CORRUPT)


async def test_suppressed_configuration_context_is_not_reported(monkeypatch):
    def suppressed():
        try:
            raise BackendConfigurationError(
                _OPERATOR_DETAIL, cause=ConfigurationCause.BACKEND_CORRUPT
            )
        except BackendConfigurationError:
            return _suppressing(RuntimeError("the real error"))

    monkeypatch.setattr(app, "_with_generation", _raising(suppressed))
    result = await _wire_call("check_access", {"agent": "a", "tool": "s::t"})

    assert result.structured_content is None
    _assert_no_operator_detail(result)


def _suppressing(exc: BaseException) -> BaseException:
    try:
        raise exc from None
    except BaseException as raised:
        return raised


async def test_cyclic_chain_still_classifies(monkeypatch):
    def cyclic():
        outer = RuntimeError("outer")
        inner = BackendConfigurationError(
            _OPERATOR_DETAIL, cause=ConfigurationCause.BACKEND_CORRUPT
        )
        outer.__cause__ = inner
        inner.__cause__ = outer
        return outer

    monkeypatch.setattr(app, "_with_generation", _raising(cyclic))
    result = await _wire_call("check_access", {"agent": "a", "tool": "s::t"})

    assert result.structured_content == _misconfigured(ConfigurationCause.BACKEND_CORRUPT)


async def test_misconfiguration_logs_one_warning_without_a_traceback(monkeypatch, caplog):
    monkeypatch.setattr(
        app,
        "_with_generation",
        _raising(
            lambda: BackendConfigurationError(
                _OPERATOR_DETAIL + "\nsecond line password=hunter2",
                cause=ConfigurationCause.BACKEND_CORRUPT,
            )
        ),
    )
    with caplog.at_level(logging.DEBUG):
        await _wire_call("check_access", {"agent": "a", "tool": "s::t"})

    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(records) == 1
    (record,) = records
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    text = record.getMessage()
    assert "\n" not in text
    assert "backend_corrupt" in text
    assert "SENTINEL_LEAK" in text  # the operator's own log keeps the path
    assert "hunter2" not in text


# Producers: each tag is set where the condition is detected, and the
# producers that are not operator configuration stay untagged.


def _native_errors():
    from neo4j.exceptions import AuthError, ConfigurationError

    return [
        (AuthError("rejected"), ConfigurationCause.NEO4J_CREDENTIALS_REJECTED),
        (
            ClientError._hydrate_neo4j(
                code="Neo.ClientError.Database.DatabaseNotFound", message="no such db"
            ),
            ConfigurationCause.NEO4J_DATABASE_NOT_FOUND,
        ),
        (
            ConfigurationError("Notification filtering is not supported for the Bolt Protocol"),
            ConfigurationCause.NEO4J_SERVER_TOO_OLD,
        ),
    ]


@pytest.mark.parametrize("index", range(3), ids=["auth", "database-not-found", "too-old"])
def test_neo4j_seam_tags_each_configuration_cause(index):
    native, cause = _native_errors()[index]

    with pytest.raises(BackendConfigurationError) as raised, driver._translating_backend_errors():
        raise native

    assert raised.value.cause is cause


def test_missing_neo4j_extra_is_tagged(monkeypatch):
    monkeypatch.setitem(sys.modules, "neo4j", None)

    with pytest.raises(BackendConfigurationError) as raised:
        driver._neo4j()

    assert raised.value.cause is ConfigurationCause.NEO4J_EXTRA_MISSING


@pytest.mark.parametrize("read_only", [False, True])
def test_missing_ladybug_is_tagged(embedded_path, monkeypatch, read_only):
    embedded_path.parent.mkdir(parents=True, exist_ok=True)
    embedded_path.write_bytes(b"")
    monkeypatch.setitem(sys.modules, "ladybug", None)
    opener = driver.acquire_readonly_session if read_only else driver.session

    with pytest.raises(BackendConfigurationError) as raised, opener():
        pass

    assert raised.value.cause is ConfigurationCause.LADYBUG_MISSING


@pytest.mark.parametrize("read_only", [False, True])
def test_corrupt_ladybug_file_is_tagged(embedded_path, read_only):
    pytest.importorskip("ladybug")
    embedded_path.parent.mkdir(parents=True, exist_ok=True)
    embedded_path.write_bytes(b"not a database" * 100)
    opener = driver.acquire_readonly_session if read_only else driver.session

    with pytest.raises(BackendConfigurationError) as raised, opener() as s:
        s.run("RETURN 1")

    assert raised.value.cause is ConfigurationCause.BACKEND_CORRUPT


def _untagged_producers():
    from toolgraph.graph import schema

    def unknown_backend():
        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(vars(config), "settings", config.Settings(backend="sqlite"))
            driver.backend_name()

    def neo4j_driver_on_ladybug():
        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(vars(config), "settings", config.Settings(backend="ladybug"))
            driver.get_driver()

    return {
        "unknown-backend": unknown_backend,
        "get-driver-guard": neo4j_driver_on_ladybug,
        "unsupported-expression": lambda: driver._ladybug_query(
            "RETURN datetime('2020-01-01') AS x"
        ),
        "schema-version": lambda: schema._refuse_version_mismatch(
            schema.BACKEND_SCHEMA_VERSION + 1
        ),
    }


@pytest.mark.parametrize("name", list(_untagged_producers()))
def test_producers_that_are_not_operator_configuration_stay_untagged(name):
    with pytest.raises(BackendConfigurationError) as raised:
        _untagged_producers()[name]()

    assert raised.value.cause is None


async def test_corrupt_ladybug_file_over_a_real_stdio_server(tmp_path):
    """The envelope and the one-line log, from a real server process."""
    pytest.importorskip("ladybug")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    path = tmp_path / "private" / "SENTINEL_LEAK.lbug"
    path.parent.mkdir()
    path.write_bytes(b"not a database" * 100)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "toolgraph.server.app"],
        env={
            "PATH": os.environ.get("PATH", ""),
            "TOOLGRAPH_CONFIG": str(tmp_path / "no-config.json"),
            "TOOLGRAPH_BACKEND": "ladybug",
            "TOOLGRAPH_DB_PATH": str(path),
        },
    )
    errlog = tmp_path / "server.err"
    with errlog.open("w") as err:
        async with (
            stdio_client(params, errlog=err) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool("check_access", {"agent": "a", "tool": "s::t"})

    assert result.is_error is True
    assert result.structured_content == _misconfigured(ConfigurationCause.BACKEND_CORRUPT)
    assert "SENTINEL_LEAK" not in result.model_dump_json()
    log = errlog.read_text()
    assert "backend_corrupt" in log
    assert "Traceback" not in log
    assert "unexpected exception" not in log

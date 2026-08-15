"""Typed MCP availability errors without weakening contract failures."""

from __future__ import annotations

import asyncio
import json

from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult
from neo4j.exceptions import (
    ClientError,
    ConnectionAcquisitionTimeoutError,
    DatabaseUnavailable,
    ServiceUnavailable,
    SessionExpired,
)
import pytest

from toolgraph.graph import driver
from toolgraph.graph.driver import (
    BackendConfigurationError,
    BackendLockedError,
    BackendUnavailableError,
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
    handler = app.mcp._mcp_server.request_handlers[CallToolRequest]
    response = await handler(
        CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(name=name, arguments=arguments),
        )
    )
    assert isinstance(response.root, CallToolResult)
    return response.root


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
    with pytest.raises(BackendUnavailableError) as raised:
        with driver._translating_backend_errors():
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

    monkeypatch.setattr(driver, "get_driver", lambda: _Driver())
    monkeypatch.setattr(driver, "backend_name", lambda: "neo4j")

    with pytest.raises(BackendUnavailableError) as raised:
        with driver.session() as current:
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

    monkeypatch.setattr(driver, "get_driver", lambda: _Driver())
    monkeypatch.setattr(driver, "backend_name", lambda: "neo4j")

    with pytest.raises(ClientError) as raised:
        with driver.session() as current:
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
            raise RuntimeError("cleanup bug")
    except RuntimeError as exc:
        assert is_backend_unavailable(exc)


@pytest.mark.parametrize("name,arguments", _TOOLS)
async def test_every_mcp_tool_returns_typed_backend_unavailable(
    monkeypatch, name, arguments
):
    def unavailable(_fetch):
        raise BackendUnavailableError(
            "bolt://alice:secret@example.test password=hunter2 sentinel-detail"
        )

    monkeypatch.setattr(app, "_with_generation", unavailable)
    result = await _wire_call(name, arguments)

    assert result.isError is True
    assert result.structuredContent == _BACKEND_PAYLOAD
    assert result.content[0].text == _BACKEND_PAYLOAD["message"]
    encoded = result.model_dump_json()
    assert "alice" not in encoded
    assert "hunter2" not in encoded
    assert "sentinel-detail" not in encoded
    assert "graph_generation" not in encoded


def test_retryable_tracks_the_advertised_annotations():
    """Retry safety must match what the server actually advertises.

    Stated as an equivalence rather than "everything is read-only" so adding a
    genuine write tool later does not make this test wrong.
    """
    tools = asyncio.run(app.mcp.list_tools())
    for tool in tools:
        declared = bool(tool.annotations and tool.annotations.readOnlyHint)
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

    assert result.isError is True
    assert result.structuredContent == {**_BACKEND_PAYLOAD, "retryable": False}
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

    assert result.structuredContent == {**_BACKEND_PAYLOAD, "retryable": False}
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
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.idempotentHint is True


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
    """FastMCP keeps the FIRST tool on a duplicate name.

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
    assert not (retained.annotations and retained.annotations.readOnlyHint)
    assert asyncio.run(server._is_retryable("same")) is False


def test_read_only_tool_rejects_conflicting_annotations():
    """The helper owns annotations; a caller override would be silently lost."""
    server = app._ToolgraphMCP("probe")
    with pytest.raises(TypeError):
        server.read_only_tool(annotations=object())


async def test_contract_error_keeps_existing_unstructured_error(monkeypatch):
    def invalid(_fetch):
        raise ValueError("unknown profile 'production'")

    monkeypatch.setattr(app, "_with_generation", invalid)
    result = await _wire_call(
        "eligible_tools",
        {"agent": "agent", "candidates": [], "profile": "production"},
    )

    assert result.isError is True
    assert result.structuredContent is None
    assert "unknown profile" in result.content[0].text


async def test_success_wire_shape_is_unchanged(monkeypatch):
    monkeypatch.setattr(app, "_with_generation", lambda _fetch: {"ok": True})
    result = await _wire_call(
        "check_access", {"agent": "agent", "tool": "server::tool"}
    )

    assert result.isError is False
    assert result.structuredContent is None
    assert json.loads(result.content[0].text) == {"ok": True}

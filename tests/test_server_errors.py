"""Typed MCP availability errors without weakening contract failures."""

from __future__ import annotations

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

from toolgraph.graph.driver import (
    BackendConfigurationError,
    BackendLockedError,
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


@pytest.mark.parametrize(
    "exc",
    [
        ServiceUnavailable("offline"),
        SessionExpired("expired"),
        ConnectionAcquisitionTimeoutError("timed out"),
        DatabaseUnavailable("unavailable"),
        BackendLockedError("locked"),
    ],
)
def test_backend_unavailable_classifier_accepts_only_known_outages(exc):
    assert is_backend_unavailable(exc)


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
    inner = ServiceUnavailable("offline")
    outer = RuntimeError("framework wrapper")
    outer.__cause__ = inner
    assert is_backend_unavailable(outer)


@pytest.mark.parametrize("name,arguments", _TOOLS)
async def test_every_mcp_tool_returns_typed_backend_unavailable(
    monkeypatch, name, arguments
):
    def unavailable(_fetch):
        raise ServiceUnavailable(
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

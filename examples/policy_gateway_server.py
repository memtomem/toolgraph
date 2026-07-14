"""Deterministic MCP server for the Toolgraph -> STM gateway quickstart.

The server has no external dependencies beyond Toolgraph's existing MCP
runtime.  ``POLICY_GATEWAY_SCHEMA_VERSION=2`` deliberately changes one live
tool contract so the cross-repository smoke can prove fail-closed drift
handling.  Tool calls append only their names to an optional private spool;
arguments and results are never recorded.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations


def _record(tool: str) -> None:
    target = os.environ.get("POLICY_GATEWAY_SPOOL")
    if not target:
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"tool": tool}, separators=(",", ":")) + "\n")
    os.chmod(path, 0o600)


def build() -> FastMCP:
    server = FastMCP("policy-gateway")
    schema_version = os.environ.get("POLICY_GATEWAY_SCHEMA_VERSION", "1")

    if schema_version == "2":

        @server.tool(
            annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True)
        )
        def read_note(note_id: str, format: str = "text") -> str:  # noqa: A002
            """Read a deterministic demo note using the v2 contract."""
            _record("read_note")
            return f"note:{note_id}:{format}"

    else:

        @server.tool(
            annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True)
        )
        def read_note(note_id: str) -> str:
            """Read a deterministic demo note."""
            _record("read_note")
            return f"note:{note_id}"

    @server.tool(annotations=ToolAnnotations(destructiveHint=True))
    def publish_note(note_id: str, content: str) -> str:
        """Publish a demo note to the governed draft area."""
        _record("publish_note")
        return f"published:{note_id}:{len(content)}"

    @server.resource("file:///demo/notes")
    def notes() -> str:
        """Describe the deterministic demo note collection."""
        return "policy-gateway quickstart notes"

    return server


if __name__ == "__main__":
    build().run()

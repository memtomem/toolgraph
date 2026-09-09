"""Deterministic, local-only MCP server for the Toolgraph quickstart."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations


def build() -> MCPServer:
    server = MCPServer("policy-gateway")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
    def read_note(note_id: str) -> str:
        """Read a deterministic demo note."""
        return f"note:{note_id}"

    @server.tool(annotations=ToolAnnotations(destructive_hint=True))
    def publish_note(note_id: str, content: str) -> str:
        """Publish a demo note to the governed draft area."""
        return f"published:{note_id}:{len(content)}"

    @server.resource("file:///demo/notes")
    def notes() -> str:
        """Describe the deterministic demo note collection."""
        return "policy-gateway quickstart notes"

    return server


if __name__ == "__main__":
    build().run()

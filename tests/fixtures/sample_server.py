"""A tiny MCP server used to exercise the crawler. Runs over stdio or http.

    python sample_server.py            # stdio (default)
    python sample_server.py http 8077  # streamable-http on :8077
"""

import sys

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations


def build() -> MCPServer:
    # mcp 2.x takes the bind address at run time rather than on the constructor.
    mcp = MCPServer("sample-server")

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
    def read_file(path: str) -> str:
        """Read a file."""
        return f"contents of {path}"

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
    def write_file(path: str, content: str) -> str:
        """Write a file."""
        return "ok"

    @mcp.resource("file:///data/customers.csv")
    def customers() -> str:
        """Customer data."""
        return "id,name\n1,acme"

    return mcp


if __name__ == "__main__":
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8077
    server = build()
    if transport == "http":
        server.run(transport="streamable-http", host="127.0.0.1", port=port)
    else:
        server.run()

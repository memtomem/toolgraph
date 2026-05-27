"""A tiny FastMCP server used to exercise the crawler. Runs over stdio or http.

    python sample_server.py            # stdio (default)
    python sample_server.py http 8077  # streamable-http on :8077
"""

import sys

from mcp.server.fastmcp import FastMCP


def build(port: int = 8077) -> FastMCP:
    mcp = FastMCP("sample-server", host="127.0.0.1", port=port)

    @mcp.tool()
    def read_file(path: str) -> str:
        """Read a file."""
        return f"contents of {path}"

    @mcp.tool()
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
    server = build(port)
    if transport == "http":
        server.run(transport="streamable-http")
    else:
        server.run()

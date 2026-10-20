"""An MCP server whose own name or one tool name holds a credential URI.

    python credential_named_server.py server   # serverInfo.name is credential-shaped
    python credential_named_server.py tool     # one tool name is credential-shaped

The crawler takes both names from what the server reports, so this is the
input boundary the identity rule has to hold at.
"""

import sys

from mcp.server.mcpserver import MCPServer

PLANTED = "neo4j://u:QX/ZQ@host/r"

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "server"
    mcp = MCPServer(PLANTED if which == "server" else "plain-server")

    @mcp.tool(name=PLANTED if which == "tool" else "read_file")
    def read_file(path: str) -> str:
        """Read a file."""
        return path

    mcp.run()

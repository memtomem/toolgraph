"""A tiny MCP server used to exercise the crawler.

    python sample_server.py                 # stdio (default)
    python sample_server.py http 8077       # streamable-http on :8077, /mcp
    python sample_server.py sse 8078        # legacy HTTP+SSE on :8078, /sse
    python sample_server.py http-auth 8079  # as http, but demands AUTH_HEADER
    python sample_server.py sse-auth 8080   # as sse, but demands AUTH_HEADER

The `-auth` modes exist so a DROPPED header fails loudly. Without them a test
that forgets to forward headers still crawls fine, which is exactly how the
header-forwarding paths stayed unverified.
"""

import sys

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

# Any header name works; this one is unlikely to collide with the SDK's own.
AUTH_HEADER = "x-toolgraph-test-token"
AUTH_TOKEN = "sekrit-crawl-token"
# Unauthenticated on purpose: this is how a test reads back the rejection tally.
RECEIPT_PATH = "/__rejections"


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


class RequireToken:
    """Reject any HTTP request that does not carry the expected auth header.

    Raw ASGI rather than Starlette's BaseHTTPMiddleware on purpose:
    BaseHTTPMiddleware buffers the response, which breaks the long-lived SSE
    stream this fixture has to serve. Non-HTTP scopes (notably `lifespan`) are
    passed straight through, or the SDK's session manager never starts.

    It also keeps a tally of rejections, readable without auth at
    `RECEIPT_PATH`. That receipt is the point: the SDK funnels both a 401 and a
    500 into the same generic error for streamable-http, so a caller inspecting
    only the exception cannot tell "refused for lack of a token" from "server
    fell over". Asking the server how many requests it turned away is direct
    evidence, and it does not depend on how the SDK maps status codes.
    """

    def __init__(self, app, token: str) -> None:
        self.app = app
        self.expected = token.encode()
        self.rejections = 0

    async def _reply(self, send, status: int, body: bytes) -> None:
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        })
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] == RECEIPT_PATH:
            await self._reply(send, 200, str(self.rejections).encode())
            return
        # dict() is last-value-wins on a repeated header. Harmless here because
        # the fixture only ever faces its own tests, but do not copy this into
        # anything that must not be confused by a duplicated header.
        supplied = dict(scope["headers"]).get(AUTH_HEADER.encode())
        if supplied != self.expected:
            self.rejections += 1
            await self._reply(send, 401, b"missing auth header")
            return
        await self.app(scope, receive, send)


def _serve_with_auth(server: MCPServer, kind: str, port: int) -> None:
    import uvicorn

    app = server.sse_app() if kind == "sse" else server.streamable_http_app()
    uvicorn.run(
        RequireToken(app, AUTH_TOKEN), host="127.0.0.1", port=port, log_level="warning"
    )


if __name__ == "__main__":
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8077
    server = build()
    if transport == "http":
        server.run(transport="streamable-http", host="127.0.0.1", port=port)
    elif transport == "sse":
        server.run(transport="sse", host="127.0.0.1", port=port)
    elif transport == "http-auth":
        _serve_with_auth(server, "http", port)
    elif transport == "sse-auth":
        _serve_with_auth(server, "sse", port)
    else:
        server.run()

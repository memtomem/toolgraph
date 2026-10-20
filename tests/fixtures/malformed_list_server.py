"""A raw stdio MCP server whose ``tools/list`` reply fails the SDK's validation.

    python malformed_list_server.py hint     # an annotation holds a string
    python malformed_list_server.py schema   # an inputSchema property key holds a URI
    python malformed_list_server.py noname   # a tool has no name and an extra key

The SDK's models do not hide their input in errors, and each reply plants a
credential URI where the error would quote it or carry it in the location.
"""

import json
import sys

PLANTED = "neo4j://u:QX ZQ@h/r"

TOOLS = {
    "hint": [
        {"name": "t", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": PLANTED}}
    ],
    "schema": [{"name": "t", "inputSchema": {"type": "object", "properties": {PLANTED: 5}}}],
    "noname": [{"inputSchema": {"type": "object"}, PLANTED: 1}],
}

if __name__ == "__main__":
    tools = TOOLS[sys.argv[1]]
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        if message["method"] == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "malformed", "version": "1"},
            }
        elif message["method"] == "tools/list":
            result = {"tools": tools}
        else:
            result = {}
        sys.stdout.write(
            json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n"
        )
        sys.stdout.flush()

#!/usr/bin/env python3
"""Deterministic MCP canary server that writes body-free terminal outcomes."""

from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer


mcp = MCPServer("gate_e")


def _record(tool: str, outcome: str) -> None:
    spool = os.environ.get("GATE_E_MCP_SPOOL")
    if not spool:
        return
    path = Path(spool)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"tool_key": f"gate_e::{tool}", "outcome": outcome},
                separators=(",", ":"),
            )
            + "\n"
        )
    os.chmod(path, 0o600)


@mcp.tool()
def always_ok() -> str:
    """Return a deterministic successful canary result."""
    _record("always_ok", "succeeded")
    return "ok"


@mcp.tool()
def always_fail() -> str:
    """Raise a deterministic synthetic failure after recording it."""
    _record("always_fail", "failed")
    raise RuntimeError("synthetic canary failure")


if __name__ == "__main__":
    mcp.run(transport="stdio")

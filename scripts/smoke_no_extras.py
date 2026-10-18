#!/usr/bin/env python3
"""Call one MCP tool on `toolgraph serve` from an install without the Neo4j extra.

The `no-extras` CI job installs the wheel with no extras and no dev group, then
runs the Ladybug walkthrough through the CLI. The CLI has no MCP client, so this
script covers the server: it starts `toolgraph serve` over stdio in the current
directory, which must already be initialized and loaded, and calls `audit_report`.
It runs with the venv's own interpreter and imports only Toolgraph and its runtime
dependencies. It fails if `neo4j` is importable at all, so it measures the install
it claims to.

    uv run --python ../.no-extras-venv --no-project python ../scripts/smoke_no_extras.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
from pathlib import Path

from toolgraph.crawler.transports import open_session
from toolgraph.models import ServerSpec


async def main() -> int:
    if importlib.util.find_spec("neo4j") is not None:
        print("FAIL: neo4j is importable; this install is not the one under test")
        return 1
    toolgraph = shutil.which("toolgraph", path=str(Path(sys.executable).parent))
    if toolgraph is None:
        print(f"FAIL: no toolgraph executable next to {sys.executable}")
        return 1
    spec = ServerSpec(name="toolgraph", transport="stdio", command=toolgraph, args=["serve"])
    async with open_session(spec) as session:
        await session.initialize()
        result = await session.call_tool("audit_report", {})
    if result.is_error:
        print(f"FAIL: audit_report returned an error: {result.content}")
        return 1
    report = result.structured_content or json.loads(result.content[0].text)
    if report.get("graph_state_verified") is not True or "status" not in report:
        print(f"FAIL: unexpected audit_report shape: {sorted(report)}")
        return 1
    if "neo4j" in sys.modules:
        print("FAIL: the client imported neo4j")
        return 1
    print(f"ok: audit_report status={report['status']} over MCP without neo4j")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

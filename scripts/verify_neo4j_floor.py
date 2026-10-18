#!/usr/bin/env python3
"""Prove the Neo4j server floor against real servers on each side of it.

The `minimum-neo4j` CI job runs the Neo4j suite on the floor (5.7) through the
test fixture. This script covers what the suite cannot: that a server below
the floor (5.6) is refused with the one documented line, by `check` and by a
read command, and that the same harness passes on 5.7. The 5.7 leg is what
proves the harness's credentials: 5.6 refuses before authentication, so its
leg passes with a wrong password too (measured).

Each container's version is read through the raw testcontainers driver before
Toolgraph runs: the refusal line names a missing Bolt feature, not a release,
so it alone cannot show which server answered. Needs Docker and the dev
dependency group::

    uv run python scripts/verify_neo4j_floor.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from testcontainers.neo4j import Neo4jContainer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._helpers import neo4j_server_version  # noqa: E402

BELOW_FLOOR = ("neo4j:5.6-community", (5, 6))
FLOOR = ("neo4j:5.7-community", (5, 7))
PASSWORD = "floor-check-password"
REFUSAL = (
    "ERROR: Neo4j server is older than 5.7, the minimum Toolgraph supports; upgrade the server"
)


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'ok' if condition else 'FAIL'}] {label}{f' — {detail}' if detail else ''}")
    if not condition:
        raise SystemExit(f"floor verification failed: {label}")


def toolgraph_executable() -> str:
    found = shutil.which("toolgraph", path=str(Path(sys.executable).parent))
    if found is None:
        raise SystemExit(f"no toolgraph entry point beside {sys.executable}")
    return found


def run_toolgraph(container: Neo4jContainer, *argv: str) -> subprocess.CompletedProcess[str]:
    # An empty working directory and an allowlisted environment: no `.env`,
    # no `.toolgraph/config.json` and no inherited TOOLGRAPH_* can answer first.
    env = {
        "PATH": str(Path(sys.executable).parent),
        "HOME": tempfile.gettempdir(),
        "TOOLGRAPH_BACKEND": "neo4j",
        "NEO4J_URI": container.get_connection_url(),
        "NEO4J_USER": "neo4j",
        "NEO4J_PASSWORD": PASSWORD,
    }
    with tempfile.TemporaryDirectory() as cwd:
        return subprocess.run(
            [toolgraph_executable(), *argv],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )


def assert_serves(container: Neo4jContainer, image: str, expected: tuple[int, int]) -> None:
    with container.get_driver() as probe:
        served = neo4j_server_version(probe)
    check(f"{image} serves Neo4j {expected[0]}.{expected[1]}", served == expected, f"got {served}")


def below_floor() -> None:
    image, expected = BELOW_FLOOR
    with Neo4jContainer(image, username="neo4j", password=PASSWORD) as container:
        assert_serves(container, image, expected)
        for command in ("check", "drift"):
            result = run_toolgraph(container, command)
            check(f"`{command}` exits 1", result.returncode == 1, f"got {result.returncode}")
            check(f"`{command}` prints nothing on stdout", result.stdout == "", repr(result.stdout))
            check(
                f"`{command}` prints exactly the refusal line",
                result.stderr.splitlines() == [REFUSAL],
                repr(result.stderr[-500:]),
            )


def at_floor() -> None:
    image, expected = FLOOR
    with Neo4jContainer(image, username="neo4j", password=PASSWORD) as container:
        assert_serves(container, image, expected)
        result = run_toolgraph(container, "check")
        check("`check` exits 0", result.returncode == 0, repr(result.stderr[-500:]))


def main() -> int:
    for name, leg in (("below the floor", below_floor), ("at the floor", at_floor)):
        print(f"{name}:")
        leg()
    print("floor verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

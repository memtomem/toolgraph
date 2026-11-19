#!/usr/bin/env python3
"""Check that a lowest-direct install put every declared floor in place.

`uv sync --resolution lowest-direct` installs the lowest version each direct
requirement allows, unless another requirement needs a higher one. Then the
floor written in pyproject.toml is installed nowhere, and the `lowest-direct`
CI job's passing suites say nothing about it: `pydantic>=2.7` resolved to
2.12.0 because mcp 2.1.1 requires it. This script fails on that case, so each
floor is either the version the job tests or gets raised.

It reads the floors from pyproject.toml and the installed versions from the
running environment. Only direct requirements with a `>=` are checked;
transitive packages are listed, not compared. Pass the same groups and extras
the environment was synced with, and run it without re-syncing, because a
plain `uv run` re-resolves a lowest-direct lock at the highest versions::

    uv sync --resolution lowest-direct --group dev --extra ladybug --extra neo4j
    uv run --no-sync python scripts/verify_lowest_direct.py \
        --group dev --extra ladybug --extra neo4j
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from collections.abc import Callable, Iterable
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]

# Direct requirements whose floor is knowingly below what resolves, each with
# the reason. Empty: every floor is currently the version that installs.
EXCEPTIONS: dict[str, str] = {}


def active_requirements(
    pyproject: dict, groups: Iterable[str], extras: Iterable[str]
) -> list[tuple[Requirement, str]]:
    """Each requirement the sync installs directly, with the extra it came from."""
    project = pyproject["project"]
    found = [(Requirement(text), "") for text in project.get("dependencies", [])]
    optional = project.get("optional-dependencies", {})
    for extra in extras:
        if extra not in optional:
            raise SystemExit(f"pyproject.toml has no extra {extra!r}")
        found += [(Requirement(text), extra) for text in optional[extra]]
    dependency_groups = pyproject.get("dependency-groups", {})
    for group in groups:
        if group not in dependency_groups:
            raise SystemExit(f"pyproject.toml has no dependency group {group!r}")
        for entry in dependency_groups[group]:
            if not isinstance(entry, str):
                raise SystemExit(f"group {group!r} includes another group; not supported")
            found.append((Requirement(entry), ""))
    return [
        (requirement, extra)
        for requirement, extra in found
        if requirement.marker is None or requirement.marker.evaluate({"extra": extra})
    ]


def floor_of(requirement: Requirement) -> Version | None:
    floors = [Version(spec.version) for spec in requirement.specifier if spec.operator == ">="]
    return max(floors) if floors else None


def check(
    requirements: Iterable[Requirement],
    installed: Callable[[str], str | None],
    exceptions: dict[str, str],
) -> tuple[list[str], list[str]]:
    """Return (report lines, problems). A problem is a floor that is not installed."""
    report: list[str] = []
    problems: list[str] = []
    for requirement in requirements:
        name = canonicalize_name(requirement.name)
        floor = floor_of(requirement)
        version = installed(name)
        if version is None:
            problems.append(f"{name}: required ({requirement}) but not installed")
            continue
        if floor is None:
            report.append(f"{name} {version} (no >= floor)")
            continue
        if Version(version) == floor:
            report.append(f"{name} {version} == floor {floor}")
        elif name in exceptions:
            report.append(f"{name} {version}, floor {floor}: excepted, {exceptions[name]}")
        else:
            report.append(f"{name} {version}, floor {floor}: MISMATCH")
            problems.append(
                f"{name}: declared floor {floor} but {version} is installed; raise the floor "
                f"in pyproject.toml to the version that resolves, or say why in EXCEPTIONS"
            )
    return report, problems


def installed_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--group", action="append", default=[])
    parser.add_argument("--extra", action="append", default=[])
    args = parser.parse_args(argv)

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [r for r, _ in active_requirements(pyproject, args.group, args.extra)]
    report, problems = check(requirements, installed_version, EXCEPTIONS)

    print("Direct requirements:")
    for line in report:
        print(f"  {line}")
    direct = {canonicalize_name(r.name) for r in requirements}
    others = sorted(
        {
            (canonicalize_name(dist.metadata["Name"]), dist.version)
            for dist in metadata.distributions()
            if canonicalize_name(dist.metadata["Name"]) not in direct
        }
    )
    print("Other installed packages (not checked):")
    for name, version in others:
        print(f"  {name} {version}")

    for problem in problems:
        print(f"ERROR: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

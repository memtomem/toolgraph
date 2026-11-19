"""scripts/verify_lowest_direct.py: which requirements it checks, and when a floor fails."""

from __future__ import annotations

import importlib.util
import shlex
import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_lowest_direct", ROOT / "scripts" / "verify_lowest_direct.py"
)
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


def _installed(versions: dict[str, str]):
    return lambda name: versions.get(name)


def test_an_installed_floor_passes_after_normalizing_the_version():
    report, problems = verify.check(
        [Requirement("neo4j>=6.2,<7"), Requirement("PyYAML>=6.0.1")],
        _installed({"neo4j": "6.2.0", "pyyaml": "6.0.1"}),
        {},
    )
    assert problems == []
    assert report == ["neo4j 6.2.0 == floor 6.2", "pyyaml 6.0.1 == floor 6.0.1"]


def test_a_floor_that_resolves_higher_fails():
    report, problems = verify.check(
        [Requirement("pydantic>=2.7,<3")], _installed({"pydantic": "2.12.0"}), {}
    )
    assert report == ["pydantic 2.12.0, floor 2.7: MISMATCH"]
    assert len(problems) == 1
    assert problems[0].startswith("pydantic: declared floor 2.7 but 2.12.0 is installed;")


def test_an_excepted_floor_is_reported_with_its_reason_and_passes():
    report, problems = verify.check(
        [Requirement("pydantic>=2.7")],
        _installed({"pydantic": "2.12.0"}),
        {"pydantic": "held for a downstream pin"},
    )
    assert problems == []
    assert report == ["pydantic 2.12.0, floor 2.7: excepted, held for a downstream pin"]


def test_a_missing_requirement_fails_and_one_without_a_floor_is_only_listed():
    report, problems = verify.check(
        [Requirement("twine>=7.0"), Requirement("pip-audit==2.10.1")],
        _installed({"pip-audit": "2.10.1"}),
        {},
    )
    assert problems == ["twine: required (twine>=7.0) but not installed"]
    assert report == ["pip-audit 2.10.1 (no >= floor)"]


def test_the_highest_of_several_floors_is_the_floor():
    _, problems = verify.check([Requirement("x>=1.0,>=1.2")], _installed({"x": "1.2"}), {})
    assert problems == []


def test_the_ci_selection_reads_the_runtime_extras_and_dev_group_of_this_pyproject():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    selected = verify.active_requirements(pyproject, ["dev"], ["ladybug", "neo4j"])
    names = {requirement.name for requirement, _ in selected}
    # One from each source the job syncs: core, the neo4j extra, the dev group.
    assert {"pydantic", "neo4j", "pytest-xdist"} <= names
    # Neither the audit group nor the notebook group is synced by the job.
    assert "pip-audit" not in names
    assert "nbclient" not in names

    without_extra = verify.active_requirements(pyproject, ["dev"], ["ladybug"])
    assert "neo4j" not in {requirement.name for requirement, _ in without_extra}


def test_an_unknown_extra_or_group_is_refused():
    pyproject = {"project": {"dependencies": []}, "dependency-groups": {}}
    with pytest.raises(SystemExit, match="no extra 'nope'"):
        verify.active_requirements(pyproject, [], ["nope"])
    with pytest.raises(SystemExit, match="no dependency group 'nope'"):
        verify.active_requirements(pyproject, ["nope"], [])


def _selection(command: str) -> list[str]:
    """The --group/--extra flags of a workflow command, in order."""
    tokens = shlex.split(command)
    return [
        f"{flag} {value}"
        for flag, value in zip(tokens, tokens[1:], strict=False)
        if flag in {"--group", "--extra"}
    ]


def test_the_lowest_direct_job_checks_the_floors_of_what_it_synced():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    runs = [step["run"] for step in workflow["jobs"]["lowest-direct"]["steps"] if "run" in step]
    syncs = [run for run in runs if run.startswith("uv sync ")]
    checks = [run for run in runs if "scripts/verify_lowest_direct.py" in run]
    assert syncs == ["uv sync --resolution lowest-direct --group dev --extra ladybug --extra neo4j"]
    assert len(checks) == 1
    assert _selection(checks[0]) == _selection(syncs[0])
    # Every later uv call keeps the lowest-direct environment.
    later = runs[runs.index(syncs[0]) + 1 :]
    assert later and all(run.startswith("uv run --no-sync ") for run in later)

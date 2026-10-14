"""Release workflow invariants: publish-job authority, no silent re-publish, a
pinned build toolchain.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _release_workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())


# Actions the publish job may run. An allowlist, because the question is not
# "did someone add a checkout" but "what runs in the job that can publish as
# this project" -- anything not named here has to be argued for.
PUBLISH_ALLOWED_ACTIONS = {
    "actions/download-artifact",
    "pypa/gh-action-pypi-publish",
}


# Commands the publish job's one shell step may invoke. Also an allowlist: a
# denylist of scary names is not a boundary, because `eval "$SUMS"` is not on
# anybody's list of scary names until it is.
PUBLISH_ALLOWED_COMMANDS = {
    "VERSION=",  # plain assignments
    "expected=",
    "actual=",
    "printf",
    "cat",
    "test",
    "sha256sum",
}


def _permissions(node: dict) -> dict:
    """Normalize a `permissions:` value.

    It can be a map, or the scalar `read-all` / `write-all`. Treating a scalar
    as "no id-token key" is how `permissions: write-all` -- which grants
    everything, id-token included -- would sail past a naive membership test.
    """
    value = node.get("permissions")
    if value is None:
        return {}
    if isinstance(value, str):
        return {"__scalar__": value}
    return value


def test_publishing_authority_is_isolated_from_build_code():
    """Nothing but the uploader may run in the job that can publish.

    This is the property that makes Trusted Publishing safe to enable: a
    compromised build script or transitive build dependency must not execute
    inside a job that can mint an OIDC token and upload as this project.

    Note what is *not* claimed. `actions/download-artifact` and the PyPI
    publisher are themselves code, pinned by commit; and ordering steps inside
    the build job is not a boundary against a build that is already
    compromised. The claim is that no checked-out project code and no installed
    dependency runs in the privileged job.
    """
    workflow = _release_workflow()
    jobs = workflow["jobs"]

    # Workflow-level permissions are inherited, and the scalar forms grant in
    # bulk -- so both the key and the scalar shorthand have to be excluded.
    top = _permissions(workflow)
    assert "id-token" not in top
    assert top.get("__scalar__") != "write-all"

    privileged = [
        name for name, job in jobs.items() if _permissions(job).get("id-token") == "write"
    ]
    assert privileged == ["publish"]

    build = jobs["build"]
    build_perms = _permissions(build)
    # Declared, not inherited: a job with no permissions block takes the
    # workflow's, so silence here is not the same as "unprivileged".
    assert build_perms and "id-token" not in build_perms
    assert build_perms.get("__scalar__") != "write-all"
    assert any("scripts/verify-artifacts.sh" in step.get("run", "") for step in build["steps"])

    publish = jobs["publish"]
    needs = publish.get("needs")
    assert needs == "build" or needs == ["build"]
    # `needs:` alone is not a gate: `if: always()` runs the job even when the
    # dependency failed, which would publish artifacts that failed verification.
    assert "if" not in publish, "the publish job must not carry a job-level if:"
    assert not publish.get("continue-on-error")
    assert (publish.get("environment") or {}).get("name") == "pypi"

    run_steps = []
    publisher_indexes = []
    for index, step in enumerate(publish["steps"]):
        assert not step.get("continue-on-error")
        assert "if" not in step or "gh-action-pypi-publish" in step.get("uses", ""), (
            "only the two publisher steps may be conditional"
        )
        if "uses" in step:
            action, _, ref = step["uses"].partition("@")
            assert action in PUBLISH_ALLOWED_ACTIONS, (
                f"{action} would run in the job that can publish"
            )
            assert re.fullmatch(r"[0-9a-f]{40}", ref), (
                f"{action} must be pinned to a commit sha, got {ref!r}"
            )
            if "gh-action-pypi-publish" in action:
                publisher_indexes.append(index)
        if "run" in step:
            run_steps.append((index, step))

    assert len(run_steps) == 1, "the publish job should run one shell step"
    digest_index, digest_step = run_steps[0]
    assert digest_index < min(publisher_indexes), (
        "the digest check must run before anything is published"
    )

    # `${{ }}` inside run: is textual substitution, so a value a compromised
    # build controls would become shell code executing right here.
    assert "${{" not in digest_step["run"]
    assert set(digest_step.get("env", {})) == {"SUMS"}

    for line in digest_step["run"].splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Structure, not commands.
        if line in {"fi", "done", "else"} or line.startswith(("if ", "while ", "for ")):
            continue
        assert any(line.startswith(allowed) for allowed in PUBLISH_ALLOWED_COMMANDS), (
            f"unexpected command in the privileged job: {line!r}"
        )


def test_production_publish_does_not_skip_existing_versions():
    """A production version that already exists is a failure, not a no-op.

    `skip-existing` is right for the TestPyPI rehearsal, which gets re-run. On
    PyPI it would turn a duplicate or partial publication into a green check.

    Keyed on each step's `if:` condition, matched exactly rather than by
    substring, so a condition that merely mentions `test-` while evaluating the
    other way cannot slip past.
    """
    steps = _release_workflow()["jobs"]["publish"]["steps"]
    publishers = [
        step for step in steps if step.get("uses", "").startswith("pypa/gh-action-pypi-publish@")
    ]
    assert len(publishers) == 2

    conditions = {step["if"].strip(): step for step in publishers}
    production = conditions["${{ !startsWith(github.ref_name, 'test-') }}"]
    rehearsal = conditions["${{ startsWith(github.ref_name, 'test-') }}"]

    assert "skip-existing" not in production["with"]
    assert "repository-url" not in production["with"]
    assert rehearsal["with"]["skip-existing"] is True
    assert rehearsal["with"]["repository-url"] == "https://test.pypi.org/legacy/"


def test_release_build_toolchain_is_pinned():
    """`test-v*` and `v*` are separate builds; both must use the same tools.

    Checked structurally -- every setup-uv in the release workflow names an
    exact version -- rather than by grepping for the string "latest".
    """
    jobs = _release_workflow()["jobs"]
    setups = [
        step for job in jobs.values() for step in job["steps"] if "setup-uv" in step.get("uses", "")
    ]
    assert setups, "the release workflow should install uv explicitly"
    for step in setups:
        version = (step.get("with") or {}).get("version", "")
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), (
            f"setup-uv version must be exact, got {version!r}"
        )

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    requires = pyproject["build-system"]["requires"]
    assert requires and all("==" in item for item in requires), (
        f"the build backend must be pinned exactly, got {requires}"
    )

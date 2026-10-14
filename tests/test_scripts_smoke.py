"""Smoke tooling: source pinning in scripts/ecosystem_smoke.py, the gateway
smoke's health counter, and how smoke runs install the dev group.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


SPEC = importlib.util.spec_from_file_location("pinned_smoke_tests", SCRIPTS / "ecosystem_smoke.py")


smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


@pytest.fixture
def source_repo(tmp_path):
    root = tmp_path / "source"
    smoke.make_target(root)
    return root, smoke.run(["git", "rev-parse", "HEAD"], cwd=root).stdout.strip()


def test_dev_dependency_group_is_not_passed_as_an_extra():
    workflow = (ROOT / ".github/workflows/policy-bundle-gateway-smoke.yml").read_text()
    operational_guide = (ROOT / "docs/gate-e-p4-operational.md").read_text()
    makefile = (ROOT / "Makefile").read_text()

    assert workflow.count("--group dev") == 2
    assert "--extra dev" not in workflow
    assert "--group dev" in operational_guide
    assert "--extra dev" not in operational_guide
    assert makefile.count("--group dev") == 2
    assert "--extra dev" not in makefile


def test_exact_sha_refuses_floating_and_missing_refs(source_repo):
    root, sha = source_repo
    assert smoke.exact_sha(root, sha) == sha
    for ref in ["HEAD", "main", sha[:12], "-" + sha[1:], "0" * 40]:
        with pytest.raises(smoke.SmokeError):
            smoke.exact_sha(root, ref)


def test_clone_excludes_dirty_overlay_and_git_environment(source_repo, tmp_path, monkeypatch):
    root, sha = source_repo
    (root / "README.md").write_text("uncommitted overlay")
    (root / "extra.txt").write_text("untracked")
    monkeypatch.setenv("GIT_DIR", str(root / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(root))
    target = tmp_path / "clone"
    smoke.clone_at(root, target, sha)
    assert (target / "README.md").read_text() == "disposable smoke target\n"
    assert not (target / "extra.txt").exists()
    assert (root / "README.md").read_text() == "uncommitted overlay"
    with pytest.raises(smoke.SmokeError, match="clean checkout"):
        smoke.require_clean_checkout(root, sha)


def test_checkout_rejects_wrong_commit(source_repo, tmp_path):
    root, sha = source_repo
    (root / "README.md").write_text("next")
    smoke.run(["git", "add", "README.md"], cwd=root)
    smoke.run(["git", "commit", "--quiet", "-m", "next"], cwd=root)
    with pytest.raises(smoke.SmokeError, match="HEAD differs"):
        smoke.require_clean_checkout(root, sha)
    smoke.clone_at(root, tmp_path / "old", sha)
    assert (tmp_path / "old" / "README.md").read_text() == "disposable smoke target\n"


def test_dependency_evidence_omits_install_paths(tmp_path, monkeypatch):
    import subprocess

    (tmp_path / "uv.lock").write_text("locked")
    monkeypatch.setattr(
        smoke,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            [],
            0,
            stdout='[{"name":"example","version":"1.0","editable_project_location":"/private/user/repo"}]',
        ),
    )
    assert smoke.dependency_evidence(tmp_path)["installed"] == [
        {"name": "example", "version": "1.0"}
    ]


def test_review_counter_requires_actual_health_evidence():
    from policy_bundle_gateway_smoke import review_counter

    assert review_counter("status\n  review would-block calls: 1\n", 1) == 1
    for report in [
        "",
        "  review would-block calls: 0",
        "  review would-block calls: 10",
        "  review would-block calls: 1\n  review would-block calls: 1",
    ]:
        with pytest.raises(Exception, match="expected would-block count"):
            review_counter(report, 1)

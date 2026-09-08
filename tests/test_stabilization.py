"""Real-backend resolution and exact-source smoke regression checks."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

from toolgraph.graph import driver, loader, queries
from toolgraph.manifest import ingest
from toolgraph.models import AccessGrant, CrawlResult, Governance, ToolRecord


class CountingSession:
    def __init__(self, session):
        self.session = session
        self.calls = []

    def run(self, query, **params):
        self.calls.append(params)
        return self.session.run(query, **params)


@pytest.mark.parametrize("size", [0, 4096, 4097])
def test_resolution_batches_and_duplicates(backend_graph, size):
    # Most keys need not exist: unknown references must be represented too.
    loader.load_crawl_result(CrawlResult(server_name="a", tools=[ToolRecord(name="same")]))
    loader.load_crawl_result(CrawlResult(server_name="b", tools=[ToolRecord(name="same")]))
    qualified = [f"a::t{i}" for i in range(size)]
    refs = qualified * 2 + (["same", "same", "absent"] if size else [])
    with driver.session() as session:
        counted = CountingSession(session)
        result = queries.resolve_tool_refs(counted, refs)
    assert len(counted.calls) == ((size + 4095) // 4096 + 1 if size else 0)
    assert all(len(next(iter(p.values()))) <= 4096 for p in counted.calls)
    assert all(result[ref] == [] for ref in qualified)
    if size:
        assert result["same"] == ["a::same", "b::same"]
        assert result["absent"] == []
    else:
        assert result == {}


def test_ingest_reuses_resolution_across_manifest_rows(backend_graph):
    from tests.test_manifest import _seed_crawl, _governance

    _seed_crawl()
    gov = _governance()
    gov.grants *= 100
    gov.data_access *= 100
    with driver.session() as session:
        def apply(tx):
            counted = CountingSession(tx)
            report = ingest._ingest(counted, gov)
            resolution_calls = [p for p in counted.calls if "keys" in p]
            # One resolve query and one drift query; repetition costs neither.
            assert len(resolution_calls) == 2
            assert all(len(p["keys"]) == 2 for p in resolution_calls)
            return report
        assert session.execute_write(apply) == ([], [])
    assert ingest.governance_counts()["can_call"] == 3


def test_ingest_diagnostics_and_rollback(backend_graph):
    from tests.test_manifest import _seed_crawl, _governance

    _seed_crawl()
    assert ingest.ingest_governance(_governance()).applied
    before = queries.graph_state()
    counts = ingest.governance_counts()
    bad = Governance(grants=[
        AccessGrant(agent="a", tool="missing::first"),
        AccessGrant(agent="a", tool="missing::second"),
        AccessGrant(agent="b", tool="missing::first"),
    ])
    report = ingest.ingest_governance(bad)
    assert len(report.warnings) == 3
    assert "missing::first" in report.warnings[0]
    assert "missing::second" in report.warnings[1]
    assert "missing::first" in report.warnings[2]
    assert queries.graph_state() == before
    assert ingest.governance_counts() == counts


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

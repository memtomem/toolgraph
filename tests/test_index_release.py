"""Reject mismatched or partially published release evidence before promotion."""
import copy
import hashlib
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("verify_index_release", Path(__file__).parents[1] / "scripts/verify_index_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.fixture
def publication(tmp_path):
    rows = []
    for name in ["toolgraph-0.0.1-py3-none-any.whl", "toolgraph-0.0.1.tar.gz"]:
        (tmp_path / name).write_bytes(name.encode())
        rows.append({"filename": name, "digests": {"sha256": hashlib.sha256(name.encode()).hexdigest()},
                     "url": "https://test-files.pythonhosted.org/" + name, "yanked": False})
    return tmp_path, {"info": {"name": "toolgraph", "version": "0.0.1"}, "urls": rows}


def test_published_and_downloaded_bytes_match_candidate(publication):
    dist, metadata = publication
    result = release.verify("0.0.1", dist, metadata, lambda url: url.rsplit("/", 1)[1].encode())
    assert result["status"] == "pass"
    assert len(result["sha256"]) == 2


@pytest.mark.parametrize("mutation", ["version", "project", "missing", "duplicate", "digest", "yanked", "url"])
def test_refuse_unsafe_or_mismatched_index(publication, mutation):
    dist, original = publication
    metadata = copy.deepcopy(original)
    if mutation == "version":
        metadata["info"]["version"] = "0.0.2"
    elif mutation == "project":
        metadata["info"]["name"] = "other"
    elif mutation == "missing":
        metadata["urls"].pop()
    elif mutation == "duplicate":
        metadata["urls"][1] = metadata["urls"][0]
    elif mutation == "digest":
        metadata["urls"][0]["digests"]["sha256"] = "0" * 64
    elif mutation == "yanked":
        metadata["urls"][0]["yanked"] = True
    elif mutation == "url":
        metadata["urls"][0]["url"] = "https://pythonhosted.org.attacker.invalid/file"
    with pytest.raises(ValueError):
        release.verify("0.0.1", dist, metadata, lambda url: url.rsplit("/", 1)[1].encode())


def test_download_mismatch_is_not_accepted(publication):
    dist, metadata = publication
    with pytest.raises(ValueError, match="downloaded bytes"):
        release.verify("0.0.1", dist, metadata, lambda url: b"different")


def test_extra_local_file_is_not_ignored(publication):
    dist, metadata = publication
    (dist / "unexpected.whl").write_bytes(b"extra")
    with pytest.raises(ValueError, match="local distribution"):
        release.verify("0.0.1", dist, metadata)

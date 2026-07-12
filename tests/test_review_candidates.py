"""G3 review-candidate CLI, sidecar safety, and graph non-mutation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import stat

import pytest
from typer.testing import CliRunner

from toolgraph import cli
from toolgraph.graph import loader, queries
from toolgraph.manifest.ingest import governance_counts, ingest_governance
from toolgraph.models import (
    AccessGrant,
    CrawlResult,
    DataAccess,
    Governance,
    Policy,
    ResourceRecord,
    ToolRecord,
)
from toolgraph.preflight import build_preflight
from toolgraph.review_candidates import (
    LoadedReviewReport,
    ReviewCandidateError,
    _writer_lock,
    annotate_candidate,
    default_annotations_path,
    list_candidates,
)


ROOT = Path(__file__).parents[1]
GOLDEN = ROOT / "contracts" / "tracegraph" / "v1.json"
CANDIDATE_ID = "167379e8-3436-546b-9dbb-5fa087ab0493"
SOURCE_DIGEST = "sha256:ce66fdf2177955ba4f7d146aadf4e7dea3f85215ece59eee003f043711469e00"
CSV = "file:///data/customers.csv"
runner = CliRunner()


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "candidates.json"
    path.write_bytes(GOLDEN.read_bytes())
    return path


def _at(hour: int) -> datetime:
    return datetime(2026, 7, 12, hour, tzinfo=timezone.utc)


def test_list_without_sidecar_is_read_only_and_open(tmp_path):
    source = _source(tmp_path)
    before = source.read_bytes()

    result = runner.invoke(cli.app, ["review-candidates", "list", str(source)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["source_report_digest"] == SOURCE_DIGEST
    assert payload["candidates"][0]["candidate_id"] == CANDIDATE_ID
    assert payload["candidates"][0]["disposition"] == "open"
    assert payload["candidates"][0]["history"] == []
    assert source.read_bytes() == before
    assert not default_annotations_path(source).exists()


def test_annotate_appends_history_and_list_filters_current_state(tmp_path):
    source = _source(tmp_path)
    sidecar = default_annotations_path(source)
    source_before = source.read_bytes()

    first = annotate_candidate(
        source,
        CANDIDATE_ID,
        disposition="accepted",
        reviewer="alice",
        note="manual policy review approved",
        recorded_at=_at(6),
    )
    second = annotate_candidate(
        source,
        CANDIDATE_ID,
        disposition="open",
        reviewer="bob",
        note="reopened for another review",
        recorded_at=_at(7),
    )
    third = annotate_candidate(
        source,
        CANDIDATE_ID,
        disposition="dismissed",
        reviewer="alice",
        recorded_at=_at(8),
    )

    assert first["disposition"] == "accepted"
    assert second["disposition"] == "open"
    assert third["disposition"] == "dismissed"
    assert [row["sequence"] for row in third["history"]] == [1, 2, 3]
    assert [row["from_disposition"] for row in third["history"]] == [
        "open",
        "accepted",
        "open",
    ]
    assert list_candidates(source, status="accepted")["candidates"] == []
    assert len(list_candidates(source, status="dismissed")["candidates"]) == 1
    assert source.read_bytes() == source_before
    assert sidecar.exists()


def test_cli_annotate_supports_override_and_emits_json(tmp_path):
    source = _source(tmp_path)
    sidecar = tmp_path / "review" / "annotations.json"
    result = runner.invoke(
        cli.app,
        [
            "review-candidates",
            "annotate",
            str(source),
            CANDIDATE_ID,
            "--status",
            "accepted",
            "--reviewer",
            "operator",
            "--annotations",
            str(sidecar),
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["disposition"] == "accepted"
    assert sidecar.exists()
    assert not default_annotations_path(source).exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--reviewer", "   "], "reviewer"),
        (["--reviewer", "operator", "--note", "token=secret"], "note"),
    ],
)
def test_cli_rejects_invalid_human_metadata_as_bad_parameters(tmp_path, args, message):
    source = _source(tmp_path)
    result = runner.invoke(
        cli.app,
        [
            "review-candidates",
            "annotate",
            str(source),
            CANDIDATE_ID,
            "--status",
            "accepted",
            *args,
        ],
    )

    assert result.exit_code == 2
    assert message in result.output
    assert not default_annotations_path(source).exists()


def test_local_review_note_may_cite_an_absolute_path(tmp_path):
    source = _source(tmp_path)
    result = runner.invoke(
        cli.app,
        [
            "review-candidates",
            "annotate",
            str(source),
            CANDIDATE_ID,
            "--status",
            "accepted",
            "--reviewer",
            "operator",
            "--note",
            "see /etc/toolgraph/policy.yaml for context",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["history"][0]["note"] == (
        "see /etc/toolgraph/policy.yaml for context"
    )


def test_stale_source_binding_fails_without_changing_sidecar(tmp_path):
    source = _source(tmp_path)
    annotate_candidate(
        source,
        CANDIDATE_ID,
        disposition="accepted",
        reviewer="operator",
        recorded_at=_at(6),
    )
    sidecar = default_annotations_path(source)
    sidecar_before = sidecar.read_bytes()
    source.write_bytes(source.read_bytes() + b"\n")

    with pytest.raises(ReviewCandidateError, match="different source"):
        list_candidates(source)

    assert sidecar.read_bytes() == sidecar_before


def test_invalid_event_chain_and_unknown_candidate_fail_closed(tmp_path):
    source = _source(tmp_path)
    annotate_candidate(
        source,
        CANDIDATE_ID,
        disposition="accepted",
        reviewer="operator",
        recorded_at=_at(6),
    )
    sidecar = default_annotations_path(source)
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["events"][0]["from_disposition"] = "dismissed"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReviewCandidateError, match="chain"):
        list_candidates(source)

    payload["events"][0]["from_disposition"] = "open"
    payload["events"][0]["candidate_id"] = "00000000-0000-4000-8000-000000000000"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReviewCandidateError, match="unknown candidate"):
        list_candidates(source)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reviewer", "operator\npassword=secret"),
        ("note", "token=do-not-echo"),
    ],
)
def test_externally_authored_sidecar_metadata_is_revalidated(
    tmp_path, field, value
):
    source = _source(tmp_path)
    annotate_candidate(
        source,
        CANDIDATE_ID,
        disposition="accepted",
        reviewer="operator",
        recorded_at=_at(6),
    )
    sidecar = default_annotations_path(source)
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["events"][0][field] = value
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReviewCandidateError) as exc_info:
        list_candidates(source)

    assert field in str(exc_info.value)
    assert "secret" not in str(exc_info.value)
    assert "do-not-echo" not in str(exc_info.value)


def test_atomic_write_failure_preserves_existing_sidecar(tmp_path, monkeypatch):
    source = _source(tmp_path)
    annotate_candidate(
        source,
        CANDIDATE_ID,
        disposition="accepted",
        reviewer="operator",
        recorded_at=_at(6),
    )
    sidecar = default_annotations_path(source)
    before = sidecar.read_bytes()

    def fail_replace(_source, _target):
        raise OSError("injected replace failure")

    monkeypatch.setattr("toolgraph.review_candidates.os.replace", fail_replace)
    with pytest.raises(ReviewCandidateError, match="cannot write"):
        annotate_candidate(
            source,
            CANDIDATE_ID,
            disposition="dismissed",
            reviewer="operator",
            recorded_at=_at(7),
        )

    assert sidecar.read_bytes() == before
    assert list(sidecar.parent.glob(f".{sidecar.name}.*.tmp")) == []


def test_atomic_write_fsyncs_file_and_parent_directory(tmp_path, monkeypatch):
    source = _source(tmp_path)
    calls: list[bool] = []
    real_fsync = os.fsync

    def tracked_fsync(descriptor):
        calls.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        return real_fsync(descriptor)

    monkeypatch.setattr("toolgraph.review_candidates.os.fsync", tracked_fsync)
    annotate_candidate(
        source,
        CANDIDATE_ID,
        disposition="accepted",
        reviewer="operator",
        recorded_at=_at(6),
    )

    assert calls == ([False] if os.name == "nt" else [False, True])


@pytest.mark.skipif(os.name == "nt", reason="directory fsync is POSIX-only")
def test_unsupported_directory_fsync_degrades_after_atomic_replace(tmp_path, monkeypatch):
    source = _source(tmp_path)
    real_fsync = os.fsync

    def unsupported_directory_fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "directory fsync unsupported")
        return real_fsync(descriptor)

    monkeypatch.setattr(
        "toolgraph.review_candidates.os.fsync", unsupported_directory_fsync
    )
    result = annotate_candidate(
        source,
        CANDIDATE_ID,
        disposition="accepted",
        reviewer="operator",
        recorded_at=_at(6),
    )

    assert result["disposition"] == "accepted"


def test_source_race_is_detected_before_sidecar_replace(tmp_path, monkeypatch):
    source = _source(tmp_path)
    from toolgraph import review_candidates as review_module

    real_load = review_module.load_report
    calls = 0

    def raced_load(path):
        nonlocal calls
        calls += 1
        loaded = real_load(path)
        if calls == 2:
            return LoadedReviewReport(
                source_report_digest="sha256:" + "0" * 64,
                candidates=loaded.candidates,
            )
        return loaded

    monkeypatch.setattr(review_module, "load_report", raced_load)
    with pytest.raises(ReviewCandidateError, match="changed while"):
        annotate_candidate(
            source,
            CANDIDATE_ID,
            disposition="accepted",
            reviewer="operator",
            recorded_at=_at(6),
        )

    assert not default_annotations_path(source).exists()


def test_concurrent_annotations_are_serialized_without_lost_events(tmp_path):
    source = _source(tmp_path)

    def write(disposition: str, reviewer: str):
        return annotate_candidate(
            source,
            CANDIDATE_ID,
            disposition=disposition,
            reviewer=reviewer,
            recorded_at=_at(6),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(write, "accepted", "alice"),
            pool.submit(write, "dismissed", "bob"),
        ]
        for future in futures:
            future.result()

    candidate = list_candidates(source)["candidates"][0]
    assert [event["sequence"] for event in candidate["history"]] == [1, 2]
    assert candidate["history"][1]["from_disposition"] == candidate["history"][0][
        "disposition"
    ]
    assert candidate["disposition"] in {"accepted", "dismissed"}


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires Windows privileges")
def test_writer_lock_uses_resolved_sidecar_path(tmp_path):
    target = tmp_path / "annotations.json"
    target.write_text("{}", encoding="utf-8")
    alias = tmp_path / "annotations-alias.json"
    alias.symlink_to(target)

    with _writer_lock(alias):
        assert Path(f"{target.resolve()}.lock").exists()
        assert not Path(f"{alias}.lock").exists()


def test_source_and_sidecar_must_be_distinct(tmp_path):
    source = _source(tmp_path)
    with pytest.raises(ReviewCandidateError, match="must not overwrite"):
        list_candidates(source, annotations_path=source)
    with pytest.raises(ReviewCandidateError, match="must not overwrite"):
        annotate_candidate(
            source,
            CANDIDATE_ID,
            disposition="accepted",
            reviewer="operator",
            annotations_path=source,
        )


def test_review_workflow_does_not_mutate_governance_graph_or_manifest(graph, tmp_path):
    loader.load_crawl_result(
        CrawlResult(
            server_name="sample",
            transport="stdio",
            tools=[ToolRecord(name="read_file")],
            resources=[ResourceRecord(uri=CSV)],
        )
    )
    report = ingest_governance(
        Governance(
            agents=["planner"],
            policies=[Policy(id="pii-deny", effect="DENY")],
            grants=[AccessGrant(agent="planner", tool="sample::read_file")],
            data_access=[
                DataAccess(tool="sample::read_file", resource=CSV, mode="READS")
            ],
            governed_by={CSV: ["pii-deny"]},
        )
    )
    assert report.warnings == []
    source = _source(tmp_path)
    manifest = ROOT / "examples" / "governance.yaml"
    manifest_before = manifest.read_bytes()
    graph_before = {
        "generation": queries.graph_generation(),
        "counts": governance_counts(),
        "blast_radius": queries.blast_radius(CSV),
        "preflight": build_preflight(
            agent="planner",
            candidates=["sample::read_file"],
            profile="review",
            run_id="g3-regression",
            created_at=_at(5),
        ),
    }

    list_candidates(source)
    annotate_candidate(
        source,
        CANDIDATE_ID,
        disposition="accepted",
        reviewer="operator",
        recorded_at=_at(6),
    )

    assert manifest.read_bytes() == manifest_before
    assert {
        "generation": queries.graph_generation(),
        "counts": governance_counts(),
        "blast_radius": queries.blast_radius(CSV),
        "preflight": build_preflight(
            agent="planner",
            candidates=["sample::read_file"],
            profile="review",
            run_id="g3-regression",
            created_at=_at(5),
        ),
    } == graph_before

"""A durability warning escalated to an error, after the write is published (#129).

The warning is raised after the atomic replace, so escalating it (``-W error``)
turns a published write into an exception. The library honours that choice;
the CLI reports it as one ``ERROR:`` line, exit 1, and keeps what it published.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import warnings
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests._helpers import ROOT, control_plan_bracket, control_plan_raw
from toolgraph import cli, config
from toolgraph.artifacts import ArtifactDurabilityWarning, atomic_write_private
from toolgraph.graph import driver
from toolgraph.review_candidates import (
    ReviewCandidateDurabilityWarning,
    annotate_candidate,
    default_annotations_path,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="directory fsync is POSIX-only")

CANDIDATE_ID = "167379e8-3436-546b-9dbb-5fa087ab0493"


@pytest.fixture
def directory_fsync_fails(monkeypatch):
    real_fsync = os.fsync

    def failing(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "injected directory I/O failure")
        return real_fsync(descriptor)

    monkeypatch.setattr("toolgraph.artifacts.os.fsync", failing)


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "candidates.json"
    shutil.copy(ROOT / "contracts" / "tracegraph" / "v1.json", path)
    return path


def _events(source: Path) -> list[dict]:
    return json.loads(default_annotations_path(source).read_text(encoding="utf-8"))["events"]


def _invoke_escalated(category: type[Warning], argv: list[str]):
    with warnings.catch_warnings():
        warnings.simplefilter("error", category)
        return CliRunner().invoke(cli.app, argv)


def _assert_one_error_line(result, *fragments: str) -> None:
    assert result.exit_code == 1, result.output
    # CliRunner keeps an unhandled exception instead of printing it, so the
    # absence of a traceback in stderr proves nothing on its own.
    assert isinstance(result.exception, SystemExit)
    assert result.stdout == ""
    lines = result.stderr.splitlines()
    assert len(lines) == 1, result.stderr
    assert lines[0].startswith("ERROR: ")
    for fragment in fragments:
        assert fragment in lines[0]


def test_escalated_sidecar_warning_raises_with_the_event_persisted(tmp_path, directory_fsync_fails):
    source = _source(tmp_path)

    with warnings.catch_warnings():
        warnings.simplefilter("error", ReviewCandidateDurabilityWarning)
        with pytest.raises(ReviewCandidateDurabilityWarning, match="do not retry"):
            annotate_candidate(
                source,
                CANDIDATE_ID,
                disposition="accepted",
                reviewer="operator",
                recorded_at=datetime(2026, 7, 12, 6, tzinfo=UTC),
            )

    events = _events(source)
    assert [(e["sequence"], e["disposition"], e["reviewer"]) for e in events] == [
        (1, "accepted", "operator")
    ]


def test_escalated_artifact_warning_raises_with_the_file_published(tmp_path, directory_fsync_fails):
    target = tmp_path / "artifact.json"

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(ArtifactDurabilityWarning, match="the file is published"):
            atomic_write_private(target, b"{}\n")

    assert target.read_bytes() == b"{}\n"


def test_cli_annotate_reports_one_line_and_records_one_event(tmp_path, directory_fsync_fails):
    source = _source(tmp_path)

    result = _invoke_escalated(
        ReviewCandidateDurabilityWarning,
        [
            "review-candidates",
            "annotate",
            str(source),
            CANDIDATE_ID,
            "--status",
            "accepted",
            "--reviewer",
            "operator",
        ],
    )

    _assert_one_error_line(result, "the event is persisted", "do not retry")
    assert len(_events(source)) == 1


def test_cli_artifact_writer_reports_one_line_and_keeps_the_file(
    tmp_path, monkeypatch, directory_fsync_fails
):
    control_plan_bracket(monkeypatch)
    plan = tmp_path / "plan.json"
    plan.write_bytes(control_plan_raw())
    out = tmp_path / "result.json"

    result = _invoke_escalated(
        ArtifactDurabilityWarning, ["control-preflight", str(plan), "--out", str(out)]
    )

    _assert_one_error_line(result, "the file is published")
    assert json.loads(out.read_text(encoding="utf-8"))["kind"] == "toolgraph.control-preflight"


def test_cli_init_keeps_the_published_config_and_its_database(
    tmp_path, monkeypatch, directory_fsync_fails, fresh_settings
):
    pytest.importorskip("ladybug")
    # `--config` below sets this module global; restore it for later tests.
    monkeypatch.setattr(config, "_explicit_config_path", None)
    state_dir = tmp_path / "state"
    db_path = state_dir.resolve() / "toolgraph.lbug"

    try:
        result = _invoke_escalated(
            ArtifactDurabilityWarning, ["init", "--state-dir", str(state_dir)]
        )

        _assert_one_error_line(result, "the file is published")
        runtime = state_dir / "config.json"
        assert json.loads(runtime.read_text(encoding="utf-8"))["db_path"] == str(db_path)
        assert db_path.exists()
        # Not rolled back: the process keeps the settings it published.
        assert config.settings.backend == "ladybug"
        assert config.settings.db_path == db_path

        # Recovery from disk, not from the settings this process kept.
        driver.close_driver()
        vars(config).pop("settings", None)
        checked = CliRunner().invoke(cli.app, ["--config", str(runtime), "check"])
    finally:
        driver.close_driver()
    assert checked.exit_code == 0, checked.output
    assert checked.stdout.startswith("OK: backend=ladybug")


def test_cli_init_neo4j_keeps_the_published_config_and_its_settings(
    tmp_path, monkeypatch, directory_fsync_fails, fresh_settings
):
    monkeypatch.setattr("toolgraph.cli.schema.init_schema", lambda: None)
    state_dir = tmp_path / "state"

    try:
        result = _invoke_escalated(
            ArtifactDurabilityWarning,
            ["init", "--backend", "neo4j", "--state-dir", str(state_dir)],
        )
    finally:
        driver.close_driver()

    _assert_one_error_line(result, "the file is published")
    runtime = json.loads((state_dir / "config.json").read_text(encoding="utf-8"))
    assert runtime["backend"] == "neo4j"
    assert config.settings.backend == "neo4j"
    assert not (state_dir / "toolgraph.lbug").exists()

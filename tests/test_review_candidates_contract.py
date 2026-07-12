"""Hermetic G3 contract tests: no Neo4j or sibling checkout required."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from toolgraph.review_candidates import (
    AnnotationReport,
    ReviewCandidateError,
    candidate_id,
    load_report,
)


ROOT = Path(__file__).parents[1]
CONTRACTS = ROOT / "contracts"
TRACEGRAPH = CONTRACTS / "tracegraph"
GOLDEN = TRACEGRAPH / "v1.json"


def test_vendored_tracegraph_golden_validates_and_has_producer_digest():
    schema = json.loads(
        (TRACEGRAPH / "review-candidates.schema.json").read_text(encoding="utf-8")
    )
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(golden)
    assert golden["candidates"][0]["artifact_digest"] == (
        "sha256:be96bf257c1c3dfe3c8e3ca756fba9e4dccee985ff350d397ec63e323c938653"
    )


def test_annotation_golden_validates_and_round_trips():
    schema = json.loads(
        (CONTRACTS / "review-annotations.schema.json").read_text(encoding="utf-8")
    )
    fixture = json.loads(
        (CONTRACTS / "fixtures" / "review-annotations-v1.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    ).validate(fixture)
    assert AnnotationReport.model_validate(fixture).model_dump(mode="json") == fixture

    without_note = json.loads(json.dumps(fixture))
    without_note["events"][0].pop("note")
    jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    ).validate(without_note)
    assert AnnotationReport.model_validate(without_note).events[0].note is None


def test_toolgraph_sidecar_rejects_unknown_fields():
    fixture = json.loads(
        (CONTRACTS / "fixtures" / "review-annotations-v1.json").read_text(
            encoding="utf-8"
        )
    )
    fixture["events"][0]["candidate"]["prompt"] = "must not survive"
    with pytest.raises(ValidationError):
        AnnotationReport.model_validate(fixture)


def test_candidate_identity_matches_syncmill_pr57():
    report = load_report(GOLDEN)
    assert candidate_id(report.candidates[0]) == "167379e8-3436-546b-9dbb-5fa087ab0493"


def test_additive_v1_fields_are_accepted_but_not_retained(tmp_path):
    payload = json.loads(GOLDEN.read_text(encoding="utf-8"))
    payload["future_envelope"] = {"prompt": "must not survive"}
    payload["candidates"][0]["future_candidate"] = {"stdout": "must not survive"}
    source = tmp_path / "additive.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    report = load_report(source)

    assert set(report.candidates[0].model_dump()) == {
        "run_id",
        "pattern_id",
        "pattern_version",
        "tool_key",
        "artifact_digest",
    }
    assert "must not survive" not in repr(report)


def test_required_candidate_and_event_collections_cannot_be_omitted(tmp_path):
    producer = json.loads(GOLDEN.read_text(encoding="utf-8"))
    producer.pop("candidates")
    source = tmp_path / "missing-candidates.json"
    source.write_text(json.dumps(producer), encoding="utf-8")

    with pytest.raises(ReviewCandidateError, match="candidates"):
        load_report(source)

    annotations = json.loads(
        (CONTRACTS / "fixtures" / "review-annotations-v1.json").read_text(
            encoding="utf-8"
        )
    )
    annotations.pop("events")
    with pytest.raises(ValidationError):
        AnnotationReport.model_validate(annotations)


@pytest.mark.parametrize(
    ("field", "value"),
    [("schema_version", 2), ("kind", "tracegraph.other")],
)
def test_unknown_major_and_kind_are_rejected_without_echoing_input(tmp_path, field, value):
    payload = json.loads(GOLDEN.read_text(encoding="utf-8"))
    payload[field] = value
    payload["secret"] = "password=do-not-echo"
    source = tmp_path / "invalid.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReviewCandidateError) as exc_info:
        load_report(source)

    assert field in str(exc_info.value)
    assert "do-not-echo" not in str(exc_info.value)


def test_duplicate_candidate_is_rejected(tmp_path):
    payload = json.loads(GOLDEN.read_text(encoding="utf-8"))
    payload["candidates"].append(dict(payload["candidates"][0]))
    source = tmp_path / "duplicate.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReviewCandidateError, match="duplicate"):
        load_report(source)

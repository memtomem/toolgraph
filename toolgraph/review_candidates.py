"""Human-review-only intake for Tracegraph review-candidate artifacts.

The Tracegraph report is immutable evidence.  Toolgraph projects only the five
body-free v1 fields and stores operator dispositions in a separate, append-only
sidecar.  This module deliberately has no graph, manifest, or selector imports.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Iterator, Literal
import uuid
import warnings

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

if os.name == "nt":  # pragma: no cover - exercised on Windows CI/users
    import msvcrt
else:  # pragma: no branch - one platform branch per interpreter
    import fcntl


TRACEGRAPH_SCHEMA_VERSION = 1
TRACEGRAPH_KIND = "tracegraph.review-candidates"
ANNOTATION_SCHEMA_VERSION = 1
ANNOTATION_KIND = "toolgraph.review-annotations"
LIST_KIND = "toolgraph.review-candidate-list"

Disposition = Literal["open", "accepted", "dismissed"]
DISPOSITIONS = frozenset({"open", "accepted", "dismissed"})

# SyncMill PR #57 made this exact-tuple UUIDv5 the ecosystem review-item
# identity.  Reusing it lets operators correlate a Toolgraph disposition with
# the corresponding SyncMill human-required board item without sharing state.
_ITEM_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://syncmill.dev/review-items/tracegraph"
)
_CREDENTIAL = re.compile(
    r"://[^/\s]+:[^/\s]+@|\b(?:password|token|api[_-]?key|credential)\s*=",
    re.IGNORECASE,
)


class ReviewCandidateError(ValueError):
    """A review report or annotation sidecar violated the G3 contract."""


class ReviewCandidateDurabilityWarning(RuntimeWarning):
    """The sidecar was replaced, but crash durability could not be confirmed."""


def _normalized_reviewer(value: str) -> str:
    reviewer = value.strip()
    if (
        not reviewer
        or len(reviewer) > 256
        or "\n" in reviewer
        or "\r" in reviewer
        or "\0" in reviewer
    ):
        raise ValueError(
            "reviewer must be a non-empty single line of at most 256 characters"
        )
    return reviewer


def _normalized_note(value: str | None) -> str | None:
    if value is None:
        return None
    note = value.strip()
    if (
        not note
        or len(note) > 500
        or "\n" in note
        or "\r" in note
        or "\0" in note
    ):
        raise ValueError(
            "note must be a non-empty single line of at most 500 characters"
        )
    if _CREDENTIAL.search(note):
        raise ValueError("note must not contain credentials")
    return note


class ReviewCandidate(BaseModel):
    """The body-free fields Toolgraph consumes from one Tracegraph finding."""

    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    run_id: str = Field(min_length=1, max_length=256, pattern=r"^\S+$")
    pattern_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    pattern_version: int = Field(ge=1)
    tool_key: str = Field(min_length=4, max_length=512, pattern=r"^\S+::\S+$")
    artifact_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class AnnotationCandidate(ReviewCandidate):
    """Closed snapshot shape used inside Toolgraph-owned sidecars."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class TracegraphReviewReport(BaseModel):
    """Supported producer envelope; additive v1 fields are discarded."""

    model_config = ConfigDict(extra="ignore", strict=True)

    schema_version: Literal[1]
    kind: Literal["tracegraph.review-candidates"]
    candidates: list[ReviewCandidate]


class AnnotationSource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1]
    kind: Literal["tracegraph.review-candidates"]
    source_report_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class AnnotationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    sequence: int = Field(ge=1)
    candidate_id: str
    candidate: AnnotationCandidate
    from_disposition: Disposition
    disposition: Disposition
    reviewer: str = Field(min_length=1, max_length=256)
    recorded_at: str
    note: str | None = Field(default=None, max_length=500)

    @field_validator("reviewer")
    @classmethod
    def _reviewer_is_safe_metadata(cls, value: str) -> str:
        normalized = _normalized_reviewer(value)
        if normalized != value:
            raise ValueError("must not contain leading or trailing whitespace")
        return value

    @field_validator("note")
    @classmethod
    def _note_is_safe_metadata(cls, value: str | None) -> str | None:
        normalized = _normalized_note(value)
        if normalized != value:
            raise ValueError("must not contain leading or trailing whitespace")
        return value

    @field_validator("candidate_id")
    @classmethod
    def _candidate_id_is_uuid(cls, value: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except ValueError as exc:
            raise ValueError("must be a UUID") from exc
        if str(parsed) != value:
            raise ValueError("must use canonical lowercase UUID form")
        return value

    @field_validator("recorded_at")
    @classmethod
    def _recorded_at_is_utc(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("must include a timezone")
        if parsed.utcoffset().total_seconds() != 0:
            raise ValueError("must be UTC")
        return value


class AnnotationReport(BaseModel):
    """Closed v1 sidecar; event history is append-only and state-chained."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    kind: Literal["toolgraph.review-annotations"]
    source: AnnotationSource
    events: list[AnnotationEvent]


class LoadedReviewReport(BaseModel):
    """Validated projection plus the exact source-byte binding."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    source_report_digest: str
    candidates: tuple[ReviewCandidate, ...]


def _safe_validation_error(kind: str, exc: ValidationError) -> ReviewCandidateError:
    first = exc.errors(include_input=False)[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or kind
    return ReviewCandidateError(f"invalid {kind} at {location}: {first['msg']}")


def _sha256(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _candidate_tuple(candidate: ReviewCandidate) -> tuple[str, str, int, str, str]:
    return (
        candidate.run_id,
        candidate.pattern_id,
        candidate.pattern_version,
        candidate.tool_key,
        candidate.artifact_digest,
    )


def candidate_id(candidate: ReviewCandidate) -> str:
    """Return the UUIDv5 used by the merged SyncMill board consumer."""
    identity = json.dumps(
        [
            TRACEGRAPH_SCHEMA_VERSION,
            TRACEGRAPH_KIND,
            *_candidate_tuple(candidate),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return str(uuid.uuid5(_ITEM_NAMESPACE, identity))


def load_report(path: str | Path) -> LoadedReviewReport:
    """Load, project, sort, and bind one complete Tracegraph report."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        detail = exc.strerror or type(exc).__name__
        raise ReviewCandidateError(f"cannot read review-candidate report: {detail}") from exc
    try:
        report = TracegraphReviewReport.model_validate_json(raw)
    except ValidationError as exc:
        raise _safe_validation_error("review-candidate report", exc) from exc

    candidates = sorted(report.candidates, key=_candidate_tuple)
    ids = [candidate_id(candidate) for candidate in candidates]
    if len(ids) != len(set(ids)):
        raise ReviewCandidateError("review-candidate report contains a duplicate candidate")
    return LoadedReviewReport(
        source_report_digest=_sha256(raw),
        candidates=tuple(candidates),
    )


def default_annotations_path(report_path: str | Path) -> Path:
    return Path(f"{Path(report_path)}.toolgraph-review.json")


def _same_path(left: Path, right: Path) -> bool:
    try:
        if left.exists() and right.exists():
            return os.path.samefile(left, right)
    except OSError:
        pass
    return left.resolve() == right.resolve()


def _new_annotations(report: LoadedReviewReport) -> AnnotationReport:
    return AnnotationReport(
        schema_version=ANNOTATION_SCHEMA_VERSION,
        kind=ANNOTATION_KIND,
        source=AnnotationSource(
            schema_version=TRACEGRAPH_SCHEMA_VERSION,
            kind=TRACEGRAPH_KIND,
            source_report_digest=report.source_report_digest,
        ),
        events=[],
    )


def _load_annotations(path: Path, report: LoadedReviewReport) -> AnnotationReport:
    if not path.exists():
        return _new_annotations(report)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        detail = exc.strerror or type(exc).__name__
        raise ReviewCandidateError(f"cannot read review annotations: {detail}") from exc
    try:
        annotations = AnnotationReport.model_validate_json(raw)
    except ValidationError as exc:
        raise _safe_validation_error("review annotations", exc) from exc
    if annotations.source.source_report_digest != report.source_report_digest:
        raise ReviewCandidateError("review annotations are bound to a different source report")
    return annotations


def _validate_events(
    annotations: AnnotationReport,
    report: LoadedReviewReport,
) -> tuple[dict[str, Disposition], dict[str, list[AnnotationEvent]]]:
    candidates = {candidate_id(candidate): candidate for candidate in report.candidates}
    states: dict[str, Disposition] = {item_id: "open" for item_id in candidates}
    histories: dict[str, list[AnnotationEvent]] = {item_id: [] for item_id in candidates}
    for expected_sequence, event in enumerate(annotations.events, start=1):
        if event.sequence != expected_sequence:
            raise ReviewCandidateError("review annotation event sequence is not contiguous")
        source_candidate = candidates.get(event.candidate_id)
        if source_candidate is None:
            raise ReviewCandidateError("review annotation references an unknown candidate")
        if (
            event.candidate.model_dump() != source_candidate.model_dump()
            or candidate_id(event.candidate) != event.candidate_id
        ):
            raise ReviewCandidateError("review annotation candidate snapshot does not match its id")
        if event.from_disposition != states[event.candidate_id]:
            raise ReviewCandidateError("review annotation disposition chain is invalid")
        states[event.candidate_id] = event.disposition
        histories[event.candidate_id].append(event)
    return states, histories


def _history_payload(event: AnnotationEvent) -> dict:
    return {
        "sequence": event.sequence,
        "from_disposition": event.from_disposition,
        "disposition": event.disposition,
        "reviewer": event.reviewer,
        "recorded_at": event.recorded_at,
        "note": event.note,
    }


def _candidate_payload(
    candidate: ReviewCandidate,
    disposition: Disposition,
    history: list[AnnotationEvent],
) -> dict:
    return {
        "candidate_id": candidate_id(candidate),
        **candidate.model_dump(mode="json"),
        "disposition": disposition,
        "history": [_history_payload(event) for event in history],
    }


def list_candidates(
    report_path: str | Path,
    *,
    annotations_path: str | Path | None = None,
    status: str | None = None,
) -> dict:
    """Return a deterministic merged view without creating or changing files."""
    if status is not None and status not in DISPOSITIONS:
        raise ReviewCandidateError(f"unknown review disposition {status!r}")
    report_file = Path(report_path)
    annotation_file = (
        Path(annotations_path)
        if annotations_path is not None
        else default_annotations_path(report_file)
    )
    if _same_path(report_file, annotation_file):
        raise ReviewCandidateError("review annotations must not overwrite the source report")
    annotation_file = annotation_file.resolve()
    report = load_report(report_file)
    annotations = _load_annotations(annotation_file, report)
    states, histories = _validate_events(annotations, report)
    candidates = []
    for candidate in report.candidates:
        item_id = candidate_id(candidate)
        if status is None or states[item_id] == status:
            candidates.append(
                _candidate_payload(candidate, states[item_id], histories[item_id])
            )
    return {
        "schema_version": ANNOTATION_SCHEMA_VERSION,
        "kind": LIST_KIND,
        "source_report_digest": report.source_report_digest,
        "candidates": candidates,
    }


def normalize_reviewer(value: str) -> str:
    try:
        return _normalized_reviewer(value)
    except ValueError as exc:
        raise ReviewCandidateError(str(exc)) from exc


def normalize_note(value: str | None) -> str | None:
    try:
        return _normalized_note(value)
    except ValueError as exc:
        raise ReviewCandidateError(str(exc)) from exc


@contextmanager
def _writer_lock(path: Path) -> Iterator[None]:
    """Serialize local writers; the OS releases this lock after a crash."""
    # Resolve aliases before deriving the lock name.  Otherwise two processes
    # can address one sidecar through real/symlink paths and take distinct locks.
    lock_path = Path(f"{path.resolve()}.lock")
    try:
        handle = lock_path.open("a+b")
    except OSError as exc:
        detail = exc.strerror or type(exc).__name__
        raise ReviewCandidateError(f"cannot lock review annotations: {detail}") from exc
    with handle:
        try:
            if os.name == "nt":  # pragma: no cover - platform-specific
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            detail = exc.strerror or type(exc).__name__
            raise ReviewCandidateError(f"cannot lock review annotations: {detail}") from exc
        try:
            yield
        finally:
            if os.name == "nt":  # pragma: no cover - platform-specific
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fsync_parent(path: Path) -> None:
    """Make the atomic rename durable on filesystems that support dir fsync."""
    if os.name == "nt":  # pragma: no cover - Windows has no directory fd
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path.parent, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        unsupported = {
            errno.EINVAL,
            errno.ENOTSUP,
            getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
            errno.EROFS,
        }
        if exc.errno not in unsupported:
            raise


def _save_atomic(annotations: AnnotationReport, path: Path) -> None:
    payload = json.dumps(
        annotations.model_dump(mode="json"), indent=2, sort_keys=True
    ) + "\n"
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            _fsync_parent(path)
        except OSError:
            # The event is already atomically visible.  Reporting total failure
            # would invite a retry that appends a duplicate event; surface the
            # narrower durability uncertainty while returning persisted state.
            warnings.warn(
                "review annotations were replaced, but parent-directory fsync "
                "failed; the event is persisted but crash durability is uncertain",
                ReviewCandidateDurabilityWarning,
                stacklevel=2,
            )
    except BaseException:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
        raise


def annotate_candidate(
    report_path: str | Path,
    item_id: str,
    *,
    disposition: str,
    reviewer: str,
    note: str | None = None,
    annotations_path: str | Path | None = None,
    recorded_at: datetime | None = None,
) -> dict:
    """Append one human disposition and return the candidate's updated view."""
    if disposition not in DISPOSITIONS:
        raise ReviewCandidateError(f"unknown review disposition {disposition!r}")
    reviewer = normalize_reviewer(reviewer)
    note = normalize_note(note)
    report_file = Path(report_path)
    annotation_file = (
        Path(annotations_path)
        if annotations_path is not None
        else default_annotations_path(report_file)
    )
    if _same_path(report_file, annotation_file):
        raise ReviewCandidateError("review annotations must not overwrite the source report")
    # From this point forward, load, lock, temp-file placement, and replacement
    # must all address one canonical target.  Replacing a symlink path would
    # otherwise split the alias from the original sidecar after the first write.
    annotation_file = annotation_file.resolve()
    try:
        annotation_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        detail = exc.strerror or type(exc).__name__
        raise ReviewCandidateError(
            f"cannot create review annotation directory: {detail}"
        ) from exc

    with _writer_lock(annotation_file):
        report = load_report(report_file)
        annotations = _load_annotations(annotation_file, report)
        states, histories = _validate_events(annotations, report)
        candidates = {candidate_id(candidate): candidate for candidate in report.candidates}
        candidate = candidates.get(item_id)
        if candidate is None:
            raise ReviewCandidateError("candidate id is not present in the source report")

        timestamp = recorded_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ReviewCandidateError("recorded_at must include a timezone")
        timestamp = timestamp.astimezone(timezone.utc)
        event = AnnotationEvent(
            sequence=len(annotations.events) + 1,
            candidate_id=item_id,
            candidate=candidate.model_dump(mode="json"),
            from_disposition=states[item_id],
            disposition=disposition,
            reviewer=reviewer,
            recorded_at=timestamp.isoformat(),
            note=note,
        )
        updated = AnnotationReport(
            schema_version=ANNOTATION_SCHEMA_VERSION,
            kind=ANNOTATION_KIND,
            source=annotations.source,
            events=[*annotations.events, event],
        )

        # The source is immutable evidence.  Catch a concurrent rewrite before
        # publishing a sidecar that claims to describe different bytes.
        if load_report(report_file).source_report_digest != report.source_report_digest:
            raise ReviewCandidateError("source report changed while annotation was being written")
        try:
            _save_atomic(updated, annotation_file)
        except OSError as exc:
            detail = exc.strerror or type(exc).__name__
            raise ReviewCandidateError(f"cannot write review annotations: {detail}") from exc

    return _candidate_payload(candidate, disposition, [*histories[item_id], event])

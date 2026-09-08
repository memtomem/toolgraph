"""Canonical, private, atomic artifact helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
import errno
import hashlib
import json
import os
from pathlib import Path
import tempfile
import warnings


class ArtifactDurabilityWarning(RuntimeWarning):
    """The artifact was replaced, but crash durability could not be confirmed."""


def rfc3339_offset_error(created_at: datetime) -> str | None:
    """Why *created_at* cannot be stamped on an artifact, or None if it can.

    Returned rather than raised so each producer keeps its own error type.
    Aware means ``utcoffset()`` returns a value, not merely that tzinfo is
    set: a tzinfo whose ``utcoffset()`` is None still isoformat()s without an
    offset, which the schemas' RFC 3339 prose forbids.
    """
    offset = created_at.utcoffset()
    if offset is None:
        return (
            "created_at must be timezone-aware — the contract requires an"
            " RFC 3339 timestamp with a UTC offset"
        )
    if offset % timedelta(minutes=1):
        return (
            f"created_at offset {offset} has sub-minute resolution — RFC 3339"
            " offsets are ±HH:MM"
        )
    return None


def canonical_json_bytes(value: object) -> bytes:
    """Encode one artifact deterministically for exact-byte digests."""
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def fsync_parent_dir(path: Path) -> None:
    """Make an atomic rename durable on filesystems that support dir fsync.

    Filesystems that cannot fsync a directory (some network/container mounts)
    report EINVAL/ENOTSUP/EROFS — those are tolerated silently because there
    is nothing more the writer can do. Any other failure propagates so the
    caller can decide whether it is fatal (the rename itself already
    published the file).
    """
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


def atomic_write_private(path: Path, payload: bytes) -> str:
    """Write 0600 bytes via fsync + atomic replace; return exact digest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = handle.name
            os.chmod(temporary, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        os.chmod(path, 0o600)
        try:
            fsync_parent_dir(path)
        except OSError:
            # The artifact is already atomically visible with the digest the
            # caller reports. Failing the whole write here would report ERROR
            # for a correct published file; surface only the narrower
            # crash-durability uncertainty.
            warnings.warn(
                "artifact was replaced, but parent-directory fsync failed; the"
                " file is published but crash durability is uncertain",
                ArtifactDurabilityWarning,
                stacklevel=2,
            )
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return sha256_bytes(payload)

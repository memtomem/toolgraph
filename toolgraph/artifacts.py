"""Canonical, private, atomic artifact helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile


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
        # Persist the directory entry where the platform exposes O_DIRECTORY.
        if hasattr(os, "O_DIRECTORY"):
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return sha256_bytes(payload)

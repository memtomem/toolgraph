"""Parse the authored governance manifest (governance.yaml)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from toolgraph.models import Governance
from toolgraph.yaml_loader import load_yaml_mapping


def load_governance(path: Path) -> Governance:
    data = load_yaml_mapping(path)
    return Governance.model_validate(data)


def governance_digest(governance: Governance) -> str:
    """Stable semantic digest of the validated authored governance model."""
    payload = json.dumps(
        governance.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

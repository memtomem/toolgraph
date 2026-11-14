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
    """Stable semantic digest of the validated authored governance model.

    The explicit target fields are left out while empty, so a manifest that
    does not use them keeps the digest it had before they existed (observation
    exports and policy bundles record it).
    """
    data = governance.model_dump(mode="json")
    for name in ("governed_tools", "governed_resources"):
        if not data.get(name):
            data.pop(name, None)
    payload = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

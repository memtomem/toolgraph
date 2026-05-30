"""Parse the authored governance manifest (governance.yaml)."""

from __future__ import annotations

from pathlib import Path

import yaml

from toolgraph.models import Governance


def load_governance(path: Path) -> Governance:
    data = yaml.safe_load(path.read_text()) or {}
    return Governance.model_validate(data)

"""Deterministic advisory preflight artifact production."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from urllib.parse import urlsplit, urlunsplit

from toolgraph.graph import queries, selector

SCHEMA_VERSION = 1
KIND = "toolgraph.preflight"
_URI = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s)]+")


def _redact_uri(value: str) -> str:
    """Remove URI userinfo and query strings without changing ordinary text."""
    if "://" not in value:
        return value
    parts = urlsplit(value)
    # Use the original netloc rather than ``parts.hostname``: urlsplit
    # lowercases hostname accessors, while graph resource identity preserves
    # the authored case. Only userinfo and query data are sensitive here.
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def redact(value):
    """Recursively scrub strings that may contain resource URI credentials."""
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        # Evidence paths contain a URI inside surrounding graph notation.
        return _URI.sub(lambda match: _redact_uri(match.group(0)), value)
    return value


def build_preflight(
    *,
    agent: str,
    candidates: list[str],
    profile: str,
    run_id: str,
    include_features: bool = False,
    created_at: datetime | None = None,
) -> dict:
    """Build one schema-v1 advisory verdict over a bracketed graph state."""
    if not run_id:
        raise ValueError("run_id must not be empty")
    if profile not in selector.PROFILES:
        raise ValueError(
            f"unknown profile {profile!r} — expected one of {sorted(selector.PROFILES)}"
        )

    def fetch() -> dict:
        filtered = selector.eligible_tools(agent, candidates, profile=profile)
        result = {
            "agent": agent,
            "agent_found": filtered["agent_found"],
            "profile": profile,
            "eligible": filtered["eligible"],
            "rejected": filtered["rejected"],
        }
        if include_features:
            result["features"] = selector.rank_features(agent, candidates)["features"]
        return result

    verdict = queries.with_graph_state(fetch, strict=True)
    if not verdict["agent_found"]:
        decision = "unresolved_identity"
    elif verdict["rejected"]:
        decision = "advisory_warn"
    else:
        decision = "advisory_allow"
    timestamp = created_at or datetime.now(timezone.utc)
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "run_id": run_id,
        "created_at": timestamp.isoformat(),
        "graph_generation": verdict["graph_generation"],
        "graph_instance_id": verdict["graph_instance_id"],
        "graph_state": verdict["graph_state"],
        "agent": agent,
        "agent_found": verdict["agent_found"],
        "profile": profile,
        "mode": "advisory",  # v1 emits advisory artifacts only
        "eligible": verdict["eligible"],
        "rejected": verdict["rejected"],
        "features": verdict.get("features", []),
    }
    artifact["decision"] = decision
    return redact(artifact)

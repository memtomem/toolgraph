"""Deterministic advisory preflight artifact production."""

from __future__ import annotations

from datetime import datetime, timezone
from toolgraph.artifacts import rfc3339_offset_error
from toolgraph.artifact_safety import safe_artifact, validate_identity
from toolgraph.artifact_safety import redact as redact

from toolgraph.graph import queries, selector

SCHEMA_VERSION = 1
KIND = "toolgraph.preflight"

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
    for identity in [agent, run_id, *candidates]:
        validate_identity(identity)
    if created_at is not None and (problem := rfc3339_offset_error(created_at)):
        raise ValueError(problem)
    if not run_id:
        raise ValueError("run_id must not be empty")
    if profile not in selector.PROFILES:
        raise ValueError(
            f"unknown profile {profile!r} — expected one of {sorted(selector.PROFILES)}"
        )

    def fetch() -> dict:
        # One graph evaluation: the filter is a pure function over the ranked
        # features, so --features must not pay a second full pass.
        ranked = selector.rank_features(agent, candidates)
        filtered = selector.filter_features(ranked, profile)
        result = {
            "agent": agent,
            "agent_found": filtered["agent_found"],
            "profile": profile,
            "eligible": filtered["eligible"],
            "rejected": filtered["rejected"],
        }
        if include_features:
            result["features"] = ranked["features"]
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
    return safe_artifact(artifact)

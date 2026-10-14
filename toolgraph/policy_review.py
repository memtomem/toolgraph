"""Non-mutating bridge from accepted Tracegraph evidence to policy work.

Trace failures are observations, not authorization facts.  This compiler makes
the hand-off explicit: it binds accepted human review dispositions to the
current graph decision and produces a private work-plan artifact.  Applying a
change still requires editing and ingesting an authored governance manifest.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from toolgraph.artifact_safety import safe_artifact, validate_identity
from toolgraph.artifacts import rfc3339_offset_error
from toolgraph.graph import queries, selector
from toolgraph.review_candidates import ReviewCandidateError, list_candidates

SCHEMA_VERSION = 1
KIND = "toolgraph.policy-review-plan"


class PolicyReviewPlanError(RuntimeError):
    """Accepted evidence cannot be projected onto a trustworthy graph state."""


def build_policy_review_plan(
    *,
    report: Path,
    agent: str,
    profile: str = selector.DEFAULT_PROFILE,
    annotations: Path | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Bind accepted Tracegraph candidates to current Toolgraph decisions."""
    validate_identity(agent)
    if created_at is not None and (problem := rfc3339_offset_error(created_at)):
        raise PolicyReviewPlanError(problem)
    if not agent.strip():
        raise PolicyReviewPlanError("agent must not be empty")
    if problem := selector.profile_error(profile):
        raise PolicyReviewPlanError(problem)
    try:
        reviewed = list_candidates(report, annotations_path=annotations, status="accepted")
    except ReviewCandidateError as exc:
        raise PolicyReviewPlanError(str(exc)) from exc
    accepted = reviewed["candidates"]
    if not accepted:
        raise PolicyReviewPlanError("the review report has no accepted candidates")

    tool_keys = sorted({row["tool_key"] for row in accepted})

    def fetch() -> dict[str, Any]:
        verdict = selector.eligible_tools(agent, tool_keys, profile)
        if not verdict["agent_found"]:
            raise PolicyReviewPlanError(f"agent {agent!r} not found in the graph")
        return verdict

    compiled = queries.with_graph_state(fetch, strict=True)
    # The plan schema requires a non-empty instance id; a graph without
    # GraphMeta would otherwise produce a schema-invalid artifact.
    instance_id = compiled["graph_state"].get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        raise PolicyReviewPlanError("graph has no instance id — run init-schema before planning")
    rejected = {row.get("tool_key") or row["candidate"]: row for row in compiled["rejected"]}
    candidates = []
    for row in accepted:
        rejection = rejected.get(row["tool_key"])
        candidates.append(
            {
                "candidate_id": row["candidate_id"],
                "run_id": row["run_id"],
                "pattern_id": row["pattern_id"],
                "pattern_version": row["pattern_version"],
                "tool_key": row["tool_key"],
                "artifact_digest": row["artifact_digest"],
                "current_decision": "rejected" if rejection else "eligible",
                "current_reason": rejection.get("reason") if rejection else None,
                "current_paths": rejection.get("paths", []) if rejection else [],
                "recommended_action": "review_governance_manifest",
                "automatic_change": False,
            }
        )

    timestamp = created_at or datetime.now(UTC)
    return safe_artifact(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "created_at": timestamp.isoformat(),
            "source_report_digest": reviewed["source_report_digest"],
            "graph_state": compiled["graph_state"],
            "agent": agent,
            "profile": profile,
            "decision_mode": "human_required",
            "candidates": candidates,
        }
    )

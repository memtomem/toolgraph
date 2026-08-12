"""Body-free orchestration control-plan validation and advisory preflight.

The plan is an upper-bound control graph supplied by an external orchestrator.
Toolgraph never executes or persists it: it validates the bounded DAG and joins
every declared principal/tool surface to the existing governance graph under one
collision-safe graph-state bracket.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from toolgraph.artifacts import sha256_bytes
from toolgraph.graph import queries, selector
from toolgraph.preflight import redact

SCHEMA_VERSION = 1
PLAN_KIND = "toolgraph.control-plan"
RESULT_KIND = "toolgraph.control-preflight"
MAX_PLAN_BYTES = 1_000_000

_QUALIFIED_TOOL = re.compile(r"^[^:\s]+::[^:\s]+$")
_FORBIDDEN_KEYS = frozenset(
    {
        "api_key",
        "body",
        "command",
        "commands",
        "credential",
        "credentials",
        "env",
        "gate_commands",
        "headers",
        "memory",
        "model_selector",
        "output",
        "outputs",
        "password",
        "patch",
        "prompt",
        "prompts",
        "repo_root",
        "repository_path",
        "secret",
        "stderr",
        "stdout",
        "token",
        "worktree",
    }
)
_FORBIDDEN_KEY_FRAGMENTS = (
    "api_key",
    "command",
    "credential",
    "memory_body",
    "model_selector",
    "output",
    "password",
    "patch",
    "prompt",
    "secret",
    "stderr",
    "stdout",
    "token",
)
_CREDENTIAL_URI = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")
_CREDENTIAL_QUERY = re.compile(
    r"[?&](?:api[_-]?key|password|secret|token)=[^&#\s]+", re.IGNORECASE
)
_ABSOLUTE_PATH = re.compile(r"^(?:/|\\\\|[A-Za-z]:[\\/])")


class ControlPlanError(ValueError):
    """The supplied plan cannot produce a trustworthy preflight artifact."""


class _AdditiveModel(BaseModel):
    """Versioned artifact model: same-major additive fields are ignored."""

    model_config = ConfigDict(extra="ignore")


class Producer(_AdditiveModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    version: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")


class Principal(_AdditiveModel):
    id: str = Field(min_length=1, max_length=256, pattern=r"^\S+$")
    candidate_tools: list[str] = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def _qualified_unique_tools(self) -> "Principal":
        bad = [tool for tool in self.candidate_tools if not _QUALIFIED_TOOL.fullmatch(tool)]
        if bad:
            raise ValueError(
                "candidate_tools must use qualified 'server::tool' keys: "
                + ", ".join(repr(tool) for tool in bad)
            )
        if len(set(self.candidate_tools)) != len(self.candidate_tools):
            raise ValueError("candidate_tools must be unique within one principal")
        return self


NodeKind = Literal["start", "agent", "router", "fanout", "join", "validator", "end"]


class ControlNode(_AdditiveModel):
    id: str = Field(min_length=1, max_length=256, pattern=r"^\S+$")
    kind: NodeKind
    principals: list[str] = Field(default_factory=list, max_length=256)
    agent_call: bool = False
    max_invocations: int | None = Field(default=None, ge=1)
    max_parallelism: int | None = Field(default=None, ge=1)
    timeout_seconds: int | None = Field(default=None, ge=1)
    validator_scope: Literal["workspace_result"] | None = None

    @model_validator(mode="after")
    def _coherent_execution_shape(self) -> "ControlNode":
        if (self.kind == "agent") != self.agent_call:
            raise ValueError(
                f"node {self.id!r} kind='agent' and agent_call must agree"
            )
        if len(set(self.principals)) != len(self.principals):
            raise ValueError(f"node {self.id!r} principals must be unique")
        if self.agent_call:
            if not self.principals:
                raise ValueError(f"agent-call node {self.id!r} requires principals")
            missing = [
                name
                for name, value in (
                    ("max_invocations", self.max_invocations),
                    ("max_parallelism", self.max_parallelism),
                    ("timeout_seconds", self.timeout_seconds),
                )
                if value is None
            ]
            if missing:
                raise ValueError(
                    f"agent-call node {self.id!r} requires {', '.join(missing)}"
                )
            assert self.max_invocations is not None
            assert self.max_parallelism is not None
            if self.max_parallelism > self.max_invocations:
                raise ValueError(
                    f"node {self.id!r} max_parallelism cannot exceed max_invocations"
                )
        elif any(
            value is not None
            for value in (self.max_invocations, self.max_parallelism, self.timeout_seconds)
        ):
            raise ValueError(
                f"non-agent-call node {self.id!r} cannot declare execution bounds"
            )
        if self.kind == "validator" and self.validator_scope is None:
            raise ValueError(f"validator node {self.id!r} requires validator_scope")
        if self.kind != "validator" and self.validator_scope is not None:
            raise ValueError(
                f"non-validator node {self.id!r} cannot declare validator_scope"
            )
        return self


EdgeKind = Literal["always", "success", "failure", "fallback", "fanout", "join"]


class ControlEdge(_AdditiveModel):
    source: str = Field(alias="from", min_length=1, max_length=256, pattern=r"^\S+$")
    target: str = Field(alias="to", min_length=1, max_length=256, pattern=r"^\S+$")
    kind: EdgeKind
    decided_by: Literal["code", "model", "human"]
    max_fan_out: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _coherent_fanout_bound(self) -> "ControlEdge":
        if self.kind != "fanout" and self.max_fan_out is not None:
            raise ValueError("max_fan_out is valid only on fanout edges")
        return self


class ControlLimits(_AdditiveModel):
    maximum_agent_calls: int = Field(ge=1)
    maximum_parallelism: int = Field(ge=1)
    maximum_cycles: Literal[0] = 0


class ControlPlan(_AdditiveModel):
    schema_version: Literal[1]
    kind: Literal["toolgraph.control-plan"]
    run_id: str = Field(min_length=1, max_length=256, pattern=r"^\S+$")
    producer: Producer
    principals: list[Principal] = Field(min_length=1, max_length=256)
    nodes: list[ControlNode] = Field(min_length=1, max_length=4096)
    edges: list[ControlEdge] = Field(max_length=16384)
    limits: ControlLimits

    @model_validator(mode="after")
    def _unique_identities(self) -> "ControlPlan":
        principal_ids = [principal.id for principal in self.principals]
        if len(set(principal_ids)) != len(principal_ids):
            raise ValueError("principal ids must be unique")
        node_ids = [node.id for node in self.nodes]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("node ids must be unique")
        known = set(principal_ids)
        missing = sorted(
            {
                principal
                for node in self.nodes
                for principal in node.principals
                if principal not in known
            }
        )
        if missing:
            raise ValueError(
                "nodes reference undeclared principals: " + ", ".join(missing)
            )
        return self


def _assert_body_free(value: object, *, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in _FORBIDDEN_KEYS or any(
                fragment in lowered for fragment in _FORBIDDEN_KEY_FRAGMENTS
            ):
                where = ".".join((*path, str(key)))
                raise ControlPlanError(f"control plan contains forbidden field {where!r}")
            _assert_body_free(child, path=(*path, str(key)))
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _assert_body_free(child, path=(*path, str(index)))
        return
    if isinstance(value, str):
        if _CREDENTIAL_URI.search(value):
            raise ControlPlanError("control plan contains a credential-bearing URI")
        if _CREDENTIAL_QUERY.search(value):
            raise ControlPlanError("control plan contains a credential-bearing query")
        if _ABSOLUTE_PATH.match(value):
            raise ControlPlanError("control plan contains an absolute filesystem path")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    value: dict = {}
    for key, child in pairs:
        if key in value:
            raise ControlPlanError(f"control plan contains duplicate key {key!r}")
        value[key] = child
    return value


def load_control_plan(raw: bytes) -> ControlPlan:
    """Parse one size-bounded, body-free, same-major additive plan."""
    if len(raw) > MAX_PLAN_BYTES:
        raise ControlPlanError(
            f"control plan exceeds the {MAX_PLAN_BYTES}-byte input limit"
        )
    try:
        decoded = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlPlanError(f"control plan is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ControlPlanError("control plan must be a JSON object")
    _assert_body_free(decoded)
    try:
        return ControlPlan.model_validate(decoded)
    except ValueError as exc:
        raise ControlPlanError(f"invalid control plan: {exc}") from exc


def _finding(code: str, *, nodes: list[str] | None = None, edges: list[int] | None = None) -> dict:
    row: dict = {"code": code}
    if nodes:
        row["nodes"] = sorted(set(nodes))
    if edges:
        row["edges"] = sorted(set(edges))
    return row


def lint_control_plan(plan: ControlPlan) -> list[dict]:
    """Return stable structural findings for the bounded v1 DAG contract."""
    findings: list[dict] = []
    nodes = {node.id: node for node in plan.nodes}
    starts = sorted(node.id for node in plan.nodes if node.kind == "start")
    ends = sorted(node.id for node in plan.nodes if node.kind == "end")
    if len(starts) != 1:
        findings.append(_finding("START_NODE_COUNT", nodes=starts))
    if len(ends) != 1:
        findings.append(_finding("END_NODE_COUNT", nodes=ends))

    unknown_edges: list[int] = []
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    reverse: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    indegree = {node_id: 0 for node_id in nodes}
    for index, edge in enumerate(plan.edges):
        if edge.source not in nodes or edge.target not in nodes:
            unknown_edges.append(index)
            continue
        adjacency[edge.source].append(edge.target)
        reverse[edge.target].append(edge.source)
        indegree[edge.target] += 1
    if unknown_edges:
        findings.append(_finding("UNKNOWN_NODE_REFERENCE", edges=unknown_edges))

    if len(starts) == 1:
        reachable: set[str] = set()
        queue = deque(starts)
        while queue:
            current = queue.popleft()
            if current in reachable:
                continue
            reachable.add(current)
            queue.extend(adjacency[current])
        missing = sorted(set(nodes) - reachable)
        if missing:
            findings.append(_finding("UNREACHABLE_NODE", nodes=missing))

    if len(ends) == 1:
        terminal: set[str] = set()
        queue = deque(ends)
        while queue:
            current = queue.popleft()
            if current in terminal:
                continue
            terminal.add(current)
            queue.extend(reverse[current])
        missing = sorted(set(nodes) - terminal)
        if missing:
            findings.append(_finding("NO_TERMINAL_PATH", nodes=missing))

    remaining = dict(indegree)
    queue = deque(sorted(node_id for node_id, degree in remaining.items() if degree == 0))
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        for target in adjacency[current]:
            remaining[target] -= 1
            if remaining[target] == 0:
                queue.append(target)
    if visited != len(nodes):
        findings.append(
            _finding(
                "CONTROL_CYCLE_UNSUPPORTED",
                nodes=[node_id for node_id, degree in remaining.items() if degree > 0],
            )
        )

    unbounded_model_fanout = [
        index
        for index, edge in enumerate(plan.edges)
        if edge.kind == "fanout" and edge.decided_by == "model" and edge.max_fan_out is None
    ]
    if unbounded_model_fanout:
        findings.append(
            _finding("MODEL_FANOUT_UNBOUNDED", edges=unbounded_model_fanout)
        )

    planned_calls = sum(
        node.max_invocations or 0 for node in plan.nodes if node.agent_call
    )
    if planned_calls > plan.limits.maximum_agent_calls:
        findings.append(
            {
                "code": "CALL_BOUND_TOO_LOW",
                "declared": plan.limits.maximum_agent_calls,
                "required": planned_calls,
            }
        )
    planned_parallelism = max(
        (node.max_parallelism or 0 for node in plan.nodes if node.agent_call),
        default=0,
    )
    if planned_parallelism > plan.limits.maximum_parallelism:
        findings.append(
            {
                "code": "PARALLEL_BOUND_TOO_LOW",
                "declared": plan.limits.maximum_parallelism,
                "required": planned_parallelism,
            }
        )
    return findings


def build_control_preflight(
    raw_plan: bytes,
    *,
    profile: str = selector.DEFAULT_PROFILE,
    created_at: datetime | None = None,
) -> dict:
    """Evaluate a valid control plan as one advisory, graph-state-bound artifact."""
    if profile not in selector.PROFILES:
        raise ControlPlanError(
            f"unknown profile {profile!r} — expected one of {sorted(selector.PROFILES)}"
        )
    plan = load_control_plan(raw_plan)
    findings = lint_control_plan(plan)

    def fetch() -> dict:
        evaluations = []
        for principal in plan.principals:
            verdict = selector.eligible_tools(
                principal.id, principal.candidate_tools, profile=profile
            )
            evaluations.append(
                {
                    "principal": principal.id,
                    "agent_found": verdict["agent_found"],
                    "eligible": verdict["eligible"],
                    "rejected": verdict["rejected"],
                }
            )
        return {"evaluations": evaluations}

    compiled = queries.with_graph_state(fetch, strict=True)
    graph_state = compiled["graph_state"]
    if not isinstance(graph_state.get("instance_id"), str) or not graph_state["instance_id"]:
        raise ControlPlanError("graph has no instance id — run init-schema before preflight")

    warned = bool(findings) or any(
        not evaluation["agent_found"] or evaluation["rejected"]
        for evaluation in compiled["evaluations"]
    )
    timestamp = created_at or datetime.now(timezone.utc)
    return redact(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": RESULT_KIND,
            "run_id": plan.run_id,
            "created_at": timestamp.isoformat(),
            "mode": "advisory",
            "profile": profile,
            "plan_digest": f"sha256:{sha256_bytes(raw_plan)}",
            "graph_state": graph_state,
            "decision": "advisory_warn" if warned else "advisory_allow",
            "evaluations": compiled["evaluations"],
            "findings": findings,
        }
    )

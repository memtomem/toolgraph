"""Strict, versioned impact artifacts; evidence is descriptive, never executable."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from toolgraph.artifact_safety import validate_identity
from toolgraph.artifacts import rfc3339_offset_error
from toolgraph.redaction import contains_credential

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Reason = Literal[
    "NOT_GRANTED",
    "DENY_VIOLATION",
    "DENY_GOVERNED",
    "DRIFTED",
    "UNMAPPED",
    "AGENT_NOT_FOUND",
    "TOOL_NOT_FOUND",
    "AMBIGUOUS_TOOL",
]
UNRESOLVED = {"AGENT_NOT_FOUND", "TOOL_NOT_FOUND", "AMBIGUOUS_TOOL"}


def clean_identity(value: str) -> str:
    if not value or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("identity must be nonblank, trimmed and free of control characters")
    validate_identity(value)
    # Inspection only: decoding must never rewrite an opaque identifier.
    if contains_credential(value) or contains_credential(unquote(value)):
        raise ValueError("identity contains credential-shaped data")
    validate_identity(unquote(value))
    return value


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class ImpactState(ContractModel):
    instance_id: str = Field(min_length=1)
    generation: int = Field(ge=0)

    @field_validator("instance_id")
    @classmethod
    def valid_instance(cls, value):
        return clean_identity(value)


class ImpactDecision(ContractModel):
    decision: Literal["eligible", "rejected", "unresolved"]
    reason: Reason | None
    tool_key: str | None
    agent_found: bool
    risk_score: float | None = Field(ge=0, le=1)
    evidence: list[dict[str, Any]]
    is_drifted: bool | None
    is_unmapped: bool | None
    has_unbacked_edges: bool | None

    @field_validator("tool_key")
    @classmethod
    def valid_key(cls, value):
        if value is not None:
            clean_identity(value)
            if value.count("::") != 1 or not all(value.split("::")):
                raise ValueError("tool_key must be server-qualified")
        return value

    @model_validator(mode="after")
    def consistent_decision(self):
        if self.decision == "unresolved":
            if self.reason not in UNRESOLVED:
                raise ValueError("unresolved decisions require an identity reason")
            if self.agent_found != (self.reason != "AGENT_NOT_FOUND"):
                raise ValueError("agent_found contradicts unresolved reason")
            if self.tool_key is not None:
                raise ValueError("unresolved decisions cannot carry a resolved tool key")
        else:
            if not self.agent_found or self.tool_key is None:
                raise ValueError("resolved decisions require an agent and qualified key")
            if self.decision == "eligible" and self.reason is not None:
                raise ValueError("eligible decisions have no rejection reason")
            if self.decision == "rejected" and (self.reason is None or self.reason in UNRESOLVED):
                raise ValueError("rejected decisions require a policy rejection reason")
        return self


def change_kind(before: ImpactDecision, after: ImpactDecision) -> str:
    if "unresolved" in (before.decision, after.decision):
        return "unresolved"
    if before.decision == after.decision:
        return "unchanged"
    return "newly_eligible" if after.decision == "eligible" else "newly_rejected"


class ImpactRow(ContractModel):
    candidate: str
    before: ImpactDecision
    after: ImpactDecision
    change: Literal["newly_eligible", "newly_rejected", "unchanged", "unresolved"]
    details_changed: bool

    @field_validator("candidate")
    @classmethod
    def valid_candidate(cls, value):
        return clean_identity(value)

    @model_validator(mode="after")
    def consistent_change(self):
        if self.change != change_kind(self.before, self.after):
            raise ValueError("change contradicts the compared decisions")
        if self.details_changed != (self.before != self.after):
            raise ValueError("details_changed contradicts compared details")
        return self


class ImpactReport(ContractModel):
    schema_version: Literal[1]
    kind: Literal["toolgraph.policy-impact"]
    mode: Literal["advisory"]
    created_at: str = Field(
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$",
        json_schema_extra={"format": "date-time"},
    )
    graph_state: ImpactState
    baseline_governance_digest: Digest
    proposed_governance_digest: Digest
    catalog_digest: Digest
    agent: str
    profile: Literal["strict", "review", "explore"]
    candidates: list[str] = Field(min_length=1, max_length=4096)
    complete: Literal[True]
    rows: list[ImpactRow] = Field(min_length=1, max_length=4096)
    regression_ready: bool
    notices: list[str]

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @field_validator("complete", mode="before")
    @classmethod
    def boolean_complete(cls, value):
        if type(value) is not bool:
            raise ValueError("complete must be a boolean")
        return value

    @field_validator("agent")
    @classmethod
    def valid_agent(cls, value):
        return clean_identity(value)

    @field_validator("created_at")
    @classmethod
    def valid_timestamp(cls, value):
        if error := rfc3339_offset_error(datetime.fromisoformat(value.replace("Z", "+00:00"))):
            raise ValueError(error)
        return value

    @model_validator(mode="after")
    def complete_scope(self):
        if self.candidates != sorted(set(self.candidates)):
            raise ValueError("candidates must be sorted and unique")
        if [row.candidate for row in self.rows] != self.candidates:
            raise ValueError("rows must cover the entire scope exactly once")
        if self.regression_ready != all(row.change != "unresolved" for row in self.rows):
            raise ValueError("regression readiness contradicts unresolved comparisons")
        return self

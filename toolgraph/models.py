"""Pydantic models: crawl inputs/outputs and authored-governance records.

Two families:
- Crawler config + factual crawl results (this phase).
- Authored governance (policies, ACL grants, data access) — used by the
  manifest ingest in a later phase, defined here so the schema lives in one place.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Transport = Literal["stdio", "streamable-http", "sse"]


# --- crawler config (servers.yaml) ---------------------------------------


class ServerSpec(BaseModel):
    """One MCP server to crawl."""

    name: str | None = None  # override; else taken from serverInfo.name
    transport: Transport = "stdio"
    # stdio
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # http / sse
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)


class ServersConfig(BaseModel):
    servers: list[ServerSpec] = Field(default_factory=list)


# --- factual crawl results (EXPOSES / PROVIDES) --------------------------


class ToolRecord(BaseModel):
    name: str
    description: str | None = None
    input_schema: dict | None = None


class ResourceRecord(BaseModel):
    uri: str
    name: str | None = None
    mime_type: str | None = None
    description: str | None = None


class CrawlResult(BaseModel):
    server_name: str
    server_version: str | None = None
    transport: Transport = "stdio"
    endpoint: str | None = None  # command line or url, for provenance
    tools: list[ToolRecord] = Field(default_factory=list)
    resources: list[ResourceRecord] = Field(default_factory=list)


# --- authored governance (governance.yaml) -------------------------------


# Provenance — per-edge metadata so a reader can tell crawled facts from
# operator assertions, and find the supporting evidence for each assertion.
# Default for authored edges is operator_asserted/high with no evidence link;
# the loader sets crawled/high/<endpoint> on EXPOSES/PROVIDES.
ProvenanceSource = Literal["crawled", "operator_asserted", "inferred"]
ProvenanceConfidence = Literal["high", "medium", "low"]


class Provenance(BaseModel):
    """Where an edge came from and how strong the claim is."""

    source: ProvenanceSource = "operator_asserted"
    confidence: ProvenanceConfidence = "high"
    # free-text pointer: file:line, URL, commit SHA, ticket id, runtime trace.
    evidence: str | None = None


class Policy(BaseModel):
    # ``extra='forbid'`` catches old manifests that still carry a node-level
    # ``provenance:`` block (the pre-completion-round shape) with a clear
    # Pydantic error pointing at the field, instead of silently ignoring it.
    # The replacement lives on GovernedByBinding (per-edge provenance).
    model_config = ConfigDict(extra="forbid")

    id: str
    effect: Literal["ALLOW", "DENY"]
    scope: str | None = None
    description: str | None = None


class AccessGrant(BaseModel):
    """Agent -[:CAN_CALL]-> Tool. Tool referenced as '<server>::<tool>' or '<tool>'."""

    agent: str
    tool: str
    granted_by: str | None = None
    provenance: Provenance | None = None


class DataAccess(BaseModel):
    """Tool -[:READS|WRITES]-> Resource."""

    tool: str
    resource: str  # resource uri
    mode: Literal["READS", "WRITES"]
    provenance: Provenance | None = None


class ExpectedException(BaseModel):
    """Operator-declared exception: this (agent, tool, resource?, policy) reach
    is intentional — surface it as ``authorized_but_governed`` rather than
    ``violation`` so reviewers don't drown in noise on by-design grants."""

    agent: str
    tool: str  # '<server>::<tool>' or bare name (same resolution rules as grants)
    policy: str
    resource: str | None = None  # narrow to a specific resource; None = any
    reason: str | None = None


class GovernedByBinding(BaseModel):
    """One (node, policy) binding with optional evidence.

    Lets authors cite WHY this resource (or tool) is bound to this policy —
    e.g. a config file, a runbook, a compliance ticket. Bare strings in
    ``governed_by`` lists are still accepted and lift to this shape with
    ``provenance=None``.
    """

    policy: str
    provenance: Provenance | None = None


class Governance(BaseModel):
    agents: list[str] = Field(default_factory=list)
    policies: list[Policy] = Field(default_factory=list)
    grants: list[AccessGrant] = Field(default_factory=list)
    data_access: list[DataAccess] = Field(default_factory=list)
    # node id -> list of bindings. Each item is either a bare policy_id string
    # (backward-compatible, no provenance) or a GovernedByBinding object with
    # per-binding source citation. node id is a resource uri or '<server>::<tool>'.
    governed_by: dict[str, list[str | GovernedByBinding]] = Field(default_factory=dict)
    expected_exceptions: list[ExpectedException] = Field(default_factory=list)


def edge_provenance_props(p: Provenance | None) -> dict:
    """Default to operator_asserted/high when authoring code omits provenance."""
    if p is None:
        return {"source": "operator_asserted", "confidence": "high", "evidence": None}
    return p.model_dump()

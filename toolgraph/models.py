"""Pydantic models: crawl inputs/outputs and authored-governance records.

Two families:
- Crawler config + factual crawl results (this phase).
- Authored governance (policies, ACL grants, data access) — used by the
  manifest ingest in a later phase, defined here so the schema lives in one place.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

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
    id: str
    effect: Literal["ALLOW", "DENY"]
    scope: str | None = None
    description: str | None = None
    provenance: Provenance | None = None


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


class Governance(BaseModel):
    agents: list[str] = Field(default_factory=list)
    policies: list[Policy] = Field(default_factory=list)
    grants: list[AccessGrant] = Field(default_factory=list)
    data_access: list[DataAccess] = Field(default_factory=list)
    # node id -> list of policy ids; node id is a resource uri or '<server>::<tool>'
    governed_by: dict[str, list[str]] = Field(default_factory=dict)


def edge_provenance_props(p: Provenance | None) -> dict:
    """Default to operator_asserted/high when authoring code omits provenance."""
    if p is None:
        return {"source": "operator_asserted", "confidence": "high", "evidence": None}
    return p.model_dump()

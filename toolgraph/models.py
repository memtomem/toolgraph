"""Pydantic models: crawl inputs/outputs and authored-governance records.

Two families:
- Crawler config + factual crawl results.
- Authored governance (policies, ACL grants, data access) — used by manifest
  ingest, defined here so the schema lives in one place.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from toolgraph.redaction import is_credential_shaped_identity
from toolgraph.uris import is_resource_ref

Transport = Literal["stdio", "streamable-http", "sse"]


def _no_key_delimiter(kind: str, value: str) -> str:
    # "::" is the Tool-key delimiter ("<server>::<tool>"). A component that
    # contains it collapses distinct identities onto one key — e.g.
    # (server="a", tool="b::c") and (server="a::b", tool="c") MERGE the same
    # node — and the ecosystem bundle schema requires exactly one delimiter.
    if "::" in value:
        raise ValueError(f"{kind} must not contain '::' (the tool-key delimiter)")
    return value


def refuse_credential_identity(kind: str, value: str) -> str:
    # Identities are stored and printed as given, so a credential in one would
    # reach every query and artifact that names it. The message names the
    # field only, and every model that calls this hides its input in errors.
    if is_credential_shaped_identity(value):
        raise ValueError(f"{kind} must not contain credential-shaped data")
    return value


def revalidated[ModelT: BaseModel](model: ModelT) -> ModelT:
    """A freshly validated copy of ``model``, for a boundary that writes it.

    Validators run only when a model is built: ``model_construct``, field
    assignment and mutating a list in place all skip them. Only such a model
    can fail here, and neither the dump's serializer warnings nor the
    validation error may describe it: both quote values, and an error inside
    a mapping carries the key. So the failure names the model only.
    """
    name = type(model).__name__
    try:
        return type(model).model_validate(model.model_dump(mode="json", warnings=False))
    except Exception:
        raise ValueError(
            f"{name} does not validate; build it with {name}.model_validate to see why"
        ) from None


class StrictInputModel(BaseModel):
    """Base for operator-authored input: unknown fields are always errors."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


# --- crawler config (servers.yaml) ---------------------------------------


class ServerSpec(StrictInputModel):
    """One MCP server to crawl."""

    name: str | None = None  # override; else taken from serverInfo.name
    transport: Transport = "stdio"

    @field_validator("name")
    @classmethod
    def _name_is_key_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return refuse_credential_identity("server name", _no_key_delimiter("server name", value))

    # stdio
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # http / sse
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)


class ServersConfig(StrictInputModel):
    """The declared fleet (``servers.yaml``).

    ``servers`` is required and must be a YAML sequence (ADR-0014). A file
    without the key, a blank or comment-only file, ``servers: null`` or a
    non-list value is rejected, so an accidentally emptied file can no
    longer request fleet retirement. ``servers: []`` is the explicit
    empty-fleet declaration: under default pruning it retires every crawled
    server; with ``--no-prune`` it keeps them. ``strict=True`` matters: lax
    validation would coerce a YAML set (``!!set {}``) to an empty list.
    """

    servers: list[ServerSpec] = Field(strict=True)


# --- factual crawl results (EXPOSES / PROVIDES) --------------------------


class ToolRecord(BaseModel):
    # Built from what a server reports: a refused name must not reach the
    # crawl failure text through the error's input.
    model_config = ConfigDict(hide_input_in_errors=True)

    name: str
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _name_is_key_safe(cls, value: str) -> str:
        return refuse_credential_identity("tool name", _no_key_delimiter("tool name", value))

    input_schema: dict | None = None
    # MCP tool annotations (ADR-0006): the server's self-claims about its own
    # behavior. Crawlable, but unverified by anyone — they surface with
    # ``confidence: medium``, never ``high`` like EXPOSES/PROVIDES.
    read_only_hint: bool | None = None
    destructive_hint: bool | None = None
    idempotent_hint: bool | None = None
    open_world_hint: bool | None = None


TOOL_HINT_FIELDS = (
    "read_only_hint",
    "destructive_hint",
    "idempotent_hint",
    "open_world_hint",
)


class ResourceRecord(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    uri: str
    name: str | None = None
    mime_type: str | None = None
    description: str | None = None


class CrawlResult(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    server_name: str
    server_version: str | None = None

    @field_validator("server_name")
    @classmethod
    def _server_name_is_key_safe(cls, value: str) -> str:
        return refuse_credential_identity("server name", _no_key_delimiter("server name", value))

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


class Provenance(StrictInputModel):
    """Where an edge came from and how strong the claim is."""

    source: ProvenanceSource = "operator_asserted"
    confidence: ProvenanceConfidence = "high"
    # free-text pointer: file:line, URL, commit SHA, ticket id, runtime trace.
    evidence: str | None = None


class Policy(StrictInputModel):
    # ``extra='forbid'`` catches old manifests that still carry a node-level
    # ``provenance:`` block (the pre-completion-round shape) with a clear
    # Pydantic error pointing at the field, instead of silently ignoring it.
    # The replacement lives on GovernedByBinding (per-edge provenance).
    id: str
    effect: Literal["ALLOW", "DENY"]
    scope: str | None = None
    description: str | None = None

    @field_validator("id")
    @classmethod
    def _id_is_credential_free(cls, value: str) -> str:
        return refuse_credential_identity("policy id", value)


class AccessGrant(StrictInputModel):
    """Agent -[:CAN_CALL]-> Tool. Tool referenced as '<server>::<tool>' or '<tool>'."""

    agent: str
    tool: str
    granted_by: str | None = None
    provenance: Provenance | None = None

    @field_validator("agent", "tool", "granted_by")
    @classmethod
    def _identity_is_credential_free(cls, value: str | None, info: ValidationInfo) -> str | None:
        return value if value is None else refuse_credential_identity(info.field_name, value)


class DataAccess(StrictInputModel):
    """Tool -[:READS|WRITES]-> Resource."""

    tool: str
    resource: str  # resource uri
    mode: Literal["READS", "WRITES"]
    provenance: Provenance | None = None

    @field_validator("tool")
    @classmethod
    def _tool_is_credential_free(cls, value: str) -> str:
        return refuse_credential_identity("tool", value)


class ExpectedException(StrictInputModel):
    """Operator-declared exception: this (agent, tool, resource?, policy) reach
    is intentional — surface it as ``authorized_but_governed`` rather than
    ``violation`` so reviewers don't drown in noise on by-design grants."""

    agent: str
    tool: str  # '<server>::<tool>' or bare name (same resolution rules as grants)
    policy: str
    resource: str | None = None  # narrow to a specific resource; None = any
    reason: str | None = None

    @field_validator("agent", "tool", "policy")
    @classmethod
    def _identity_is_credential_free(cls, value: str, info: ValidationInfo) -> str:
        return refuse_credential_identity(info.field_name, value)


class GovernedByBinding(StrictInputModel):
    """One (node, policy) binding with optional evidence.

    Lets authors cite WHY this resource (or tool) is bound to this policy —
    e.g. a config file, a runbook, a compliance ticket. Bare strings in
    ``governed_by`` lists are still accepted and lift to this shape with
    ``provenance=None``.
    """

    policy: str
    provenance: Provenance | None = None


class Governance(StrictInputModel):
    agents: list[str] = Field(default_factory=list)
    policies: list[Policy] = Field(default_factory=list)
    grants: list[AccessGrant] = Field(default_factory=list)
    data_access: list[DataAccess] = Field(default_factory=list)
    # node id -> list of bindings. Each item is either a bare policy_id string
    # (backward-compatible, no provenance) or a GovernedByBinding object with
    # per-binding source citation. node id is a resource uri or '<server>::<tool>'.
    governed_by: dict[str, list[str | GovernedByBinding]] = Field(default_factory=dict)
    expected_exceptions: list[ExpectedException] = Field(default_factory=list)

    @field_validator("agents")
    @classmethod
    def _agents_are_credential_free(cls, value: list[str]) -> list[str]:
        for agent in value:
            refuse_credential_identity("agent", agent)
        return value

    @model_validator(mode="after")
    def _governed_by_is_credential_free(self) -> Governance:
        # Checked here rather than per field: an error raised inside the
        # mapping would carry its key in the error location, which hiding the
        # input does not cover. A resource key is a resource, not an identity.
        for node, bindings in self.governed_by.items():
            if not is_resource_ref(node):
                refuse_credential_identity("governed_by tool ref", node)
            for binding in bindings:
                policy = binding if isinstance(binding, str) else binding.policy
                refuse_credential_identity("governed_by policy id", policy)
        return self


def edge_provenance_props(p: Provenance | None) -> dict:
    """Default to operator_asserted/high when authoring code omits provenance."""
    if p is None:
        return {"source": "operator_asserted", "confidence": "high", "evidence": None}
    return p.model_dump()
